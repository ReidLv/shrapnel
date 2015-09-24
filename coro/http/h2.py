# -*- Mode: Python -*-

import struct
import coro
import sys
import os

# Note: not trying to share code with spdy, since the plan is to deprecate it completely.

from coro.http import connection, tlslite_server, openssl_server, s2n_server, http_request
from coro.http.protocol import header_set, http_file, latch
from coro.http.hpack import Encoder, Decoder
from coro import read_stream

from coro.log import Facility
LOG = Facility ('h2')

# XXX address MAX_FRAME_SIZE here.

class h2_file (http_file):

    # override http_file's content generator (which is a 'pull' generator)
    #   with this coro.fifo-based 'push' generator.

    def __init__ (self, headers, stream):
        self.streami = stream
        self.streamo = read_stream.buffered_stream (self.get_content_gen().next)
        self.done_cv = latch()

    def get_content_gen (self):
        self.content_fifo = coro.fifo()
        return self._gen_h2()

    def _gen_h2 (self):
        while 1:
            block = self.content_fifo.pop()
            if block is None:
                # LOG ('gen_h2: end of content')
                self.done_cv.wake_all()
                break
            else:
                yield block

class h2_server_request (http_request):

    def __init__ (self, flags, stream_id, client, headers):
        self.fin_sent = False
        self.flags = flags
        self.stream_id = stream_id
        self.pending_data_frame = None
        method = headers.get_one (':method').lower()
        scheme = headers.get_one (':scheme')
        host   = headers.get_one (':host')
        path   = headers.get_one (':path')
        version = headers.get_one (':version')
        # left off by chrome now?
        headers['host'] = host
        # XXX proxy
        # url = '%s://%s/%s' % (scheme, host, path)
        url = path
        # XXX consider changing the api to take these as separate arguments
        request = '%s %s %s' % (method, url, version)
        # XXX consider removing method/url/version?
        http_request.__init__ (self, client, request, headers)
        if self.has_body():
            self.make_content_file()

    def can_deflate (self):
        return True

    def has_body (self):
        LOG ('has_body', not (self.flags & FLAGS.END_STREAM))
        return not (self.flags & FLAGS.END_STREAM)

    def make_content_file (self):
        # XXX probably untested...
        self.file = h2_file (self.request_headers, self.client.stream)

    def push_headers (self, has_data=False):
        reason = self.responses[self.reply_code]
        #self.reply_headers[':status'] = '%d %s' % (self.reply_code, reason)
        self.reply_headers[':status'] = '%d' % (self.reply_code,)
        #self.reply_headers[':version'] = 'HTTP/1.1'
        self.client.push_headers (self, has_data)
        self.sent_headers = True

    def push_data (self, data, last=False):
        # we hold back one frame in order to be able to set FLAGS_END_STREAM on the last one.
        if self.pending_data_frame is None:
            self.pending_data_frame = data
        else:
            self.pending_data_frame, data = data, self.pending_data_frame
            self.client.push_data (self, data, last)

    def push (self, data, flush=False):
        "push output data for this request."
        if not self.sent_headers:
            self.push_headers (has_data=data)
        if self.deflate:
            if data:
                data = self.deflate.compress (data)
        if data:
            self.push_data (data)

    def done (self):
        if not self.sent_headers:
            self.push_syn_reply (has_data=False)
        else:
            if self.deflate:
                self.push_data (self.deflate.flush())
            self.push_data (None, last=True)
        http_request.done (self)

def unpack_frame_header (head):
    lentype, flags, stream_id = struct.unpack ('>LBl', head)
    assert stream_id >= 0
    return lentype >> 8, lentype & 0xff, flags, stream_id

def pack_frame_header (length, ftype, flags, stream_id):
    lentype = (length << 8) | (ftype & 0xff)
    return struct.pack ('>LBl', lentype, flags, stream_id)

class ERROR:
    NO_ERROR            = 0x0
    PROTOCOL_ERROR      = 0x1
    INTERNAL_ERROR      = 0x2
    FLOW_CONTROL_ERROR  = 0x3
    SETTINGS_TIMEOUT    = 0x4
    STREAM_CLOSED       = 0x5
    FRAME_SIZE_ERROR    = 0x6
    REFUSED_STREAM      = 0x7
    CANCEL              = 0x8
    COMPRESSION_ERROR   = 0x9
    CONNECT_ERROR       = 0xa
    ENHANCE_YOUR_CALM   = 0xb
    INADEQUATE_SECURITY = 0xc
    HTTP_1_1_REQUIRED   = 0xd

class FRAME:
    DATA          = 0
    HEADERS       = 1
    PRIORITY      = 2
    RST_STREAM    = 3
    SETTINGS      = 4
    PUSH_PROMISE  = 5
    PING          = 6
    GOAWAY        = 7
    WINDOW_UPDATE = 8
    CONTINUATION  = 9
    types = {
        0: 'data',
        1: 'headers',
        2: 'priority',
        3: 'rst_stream',
        4: 'settings',
        5: 'push_promise',
        6: 'ping',
        7: 'goaway',
        8: 'window_update',
        9: 'continuation',
    }

class SETTINGS:
    HEADER_TABLE_SIZE      = 0x01
    ENABLE_PUSH            = 0x02
    MAX_CONCURRENT_STREAMS = 0x03
    INITIAL_WINDOW_SIZE    = 0x04
    MAX_FRAME_SIZE         = 0x05
    MAX_HEADER_LIST_SIZE   = 0x06

class FLAGS:
    END_STREAM   = 0x01
    END_HEADERS  = 0x04
    PADDED       = 0x08
    PRIORITY     = 0x20
    PING_ACK     = 0x01
    SETTINGS_ACK = 0x01

# this is a mixin class used for both server and client.

class h2_protocol:

    protocol = 'h2'
    preface = 'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
    is_server = True

    # for those socket types not implementing read_exact.
    def read_exact (self, size):
        try:
            return self.conn.read_exact (size)
        except AttributeError:
            left = size
            r = []
            while left:
                block = self.conn.recv (left)
                if not block:
                    break
                else:
                    r.append (block)
                    left -= len (block)
            return ''.join (r)

    def read_frames (self):
        if self.is_server:
            preface = self.read_exact (len (self.preface))
            if not preface:
                self.close()
                return
            else:
                LOG ('preface', preface)
                assert (preface == self.preface)
        try:
            while 1:
                head = self.read_exact (9)
                if not head:
                    self.close()
                    return
                flen, ftype, flags, stream_id = unpack_frame_header (head)
                LOG ('frame header', flen, ftype, flags, stream_id)
                if flen:
                    payload = self.read_exact (flen)
                else:
                    payload = b''
                method_name = 'frame_%s' % (FRAME.types.get (ftype, ''))
                if method_name == 'frame_':
                    self.log ('unknown h2 frame type: %d' % (ftype,))
                else:
                    LOG ('frame', method_name, flags, stream_id, payload)
                    method = getattr (self, method_name)
                    method (flags, stream_id, payload)
        except OSError:
            LOG ('OSError')
            self.close()

# --------------------------------------------------------------------------------
#                             h2 server
# --------------------------------------------------------------------------------

# XXX not a fan of multiple inheritance, but this seems to be the cleanest way to share
# XXX the code between server and client...

class h2_connection (h2_protocol, connection):

    protocol = 'h2'
    # default to 400K buffered output
    output_buffer_size = 400 * 1024
    h2_settings = None
    last_ping = None

    def run (self):
        self.streams = {}
        self.priorities = {}
        self.encoder = Encoder()
        self.decoder = Decoder()
        self.ofifo = coro.fifo()
        self.obuf = coro.semaphore (self.output_buffer_size)
        coro.spawn (self.send_thread)
        self.push_settings()
        try:
            self.read_frames()
        except coro.oserrors.ECONNRESET:
            LOG ('connection reset')
        finally:
            self.ofifo.push (None)

    def close (self):
        self.ofifo.push (None)
        self.conn.close()

    def send_thread (self):
        try:
            done = False
            while not done:
                blocks = self.ofifo.pop_all()
                if None in blocks:
                    done = True
                    blocks = [x for x in blocks if x is not None]
                if blocks:
                    total_size = sum ([len(x) for x in blocks])
                    LOG ('send', total_size)
                    self.conn.writev (blocks)
                    self.obuf.release (total_size)
        except OSError:
            LOG ('OSError')
        finally:
            self.close()

    def push_frame (self, frame):
        self.obuf.acquire (len(frame))
        self.ofifo.push (frame)
        return len(frame)

    def send_frame (self, ftype, flags, stream_id, data):
        dlen = len(data)
        head = pack_frame_header (dlen, ftype, flags, stream_id)
        LOG ('send_frame', FRAME.types[ftype], flags, stream_id)
        return self.push_frame (head + data)

    def push_headers (self, req, has_data):
        LOG ('push_headers', req, not not has_data)
        flags = FLAGS.END_HEADERS
        if not has_data:
            flags |= FLAGS.END_STREAM
        hdata = self.pack_http_header (req.reply_headers)
        LOG ('push_headers', req.stream_id)
        req.output.sent += self.send_frame (0x01, flags, req.stream_id, hdata)

    def push_ping (self, flags=0, data=None):
        if data is None:
            data = os.urandom (8)
        assert len(data) == 8
        self.last_ping = data
        self.send_frame (0x06, flags, 0, data)

    def push_data (self, req, data, last):
        if last:
            flags = FLAGS.END_STREAM
        else:
            flags = 0
        LOG ('push_data', len(data))
        req.output.sent += self.send_frame (0x00, flags, req.stream_id, data)

    def frame_settings (self, flags, stream_id, payload):
        plen = len(payload)
        n, check = divmod (plen, 6)
        if flags & FLAGS.SETTINGS_ACK:
            LOG ('settings', 'ack')
        else:
            assert check == 0
            # XXX store these into ivars
            self.h2_settings = {}
            for i in range (0, plen, 6):
                ident, value = struct.unpack ('>HL', payload[i:i+6])
                self.h2_settings[ident] = value
            LOG ('settings', self.h2_settings)
            # ack it.
            self.send_frame (FRAME.SETTINGS, FLAGS.SETTINGS_ACK, 0, '')

    initial_window_size = 65535
    initial_settings = [
        (SETTINGS.INITIAL_WINDOW_SIZE, initial_window_size),
    ]
    def push_settings (self):
        payload = []
        for key, val in self.initial_settings:
            payload.append (struct.pack ('>HL', key, val))
        self.send_frame (FRAME.SETTINGS, 0, 0, b''.join (payload))

    def frame_ping (self, flags, stream_id, payload):
        assert len(payload) == 8
        LOG ('ping', flags, stream_id, payload)
        if flags & FLAGS.PING_ACK:
            assert payload == self.last_ping
        else:
            assert len(payload) == 8
            self.send_frame (0x06, FLAGS.PING_ACK, 0, payload)

    def frame_window_update (self, flags, stream_id, payload):
        increment, = struct.unpack ('>l', payload)
        assert increment >= 0
        LOG ('window_update', increment)

    def frame_headers (self, flags, stream_id, payload):
        pos = 0
        pad_len = 0
        stream_dep = 0
        weight = 0
        assert stream_id > 0
        if flags & FLAGS.PADDED:
            pad_len, = struct.unpack ('>B', payload[pos:pos+1])
            pos += 1
            #LOG ('headers', 'padded', pad_len)
        if flags & FLAGS.PRIORITY:
            stream_dep, weight = struct.unpack ('>lB', payload[:5])
            pos += 5
            #LOG ('headers', 'priority', stream_dep, weight)
        if flags & FLAGS.END_STREAM:
            #LOG ('headers', 'end_stream')
            pass
        if flags & FLAGS.END_HEADERS:
            #LOG ('headers', 'end_headers')
            pass
        else:
            raise NotImplementedError
        LOG ('headers', flags, stream_id, pad_len, stream_dep, weight)
        if pad_len:
            header_block = payload[pos:-pad_len]
        else:
            header_block = payload[pos:]
        headers = self.unpack_http_header (header_block)
        req = h2_server_request (flags, stream_id, self, headers)
        self.streams[stream_id] = req
        coro.spawn (self.handle_request, req)

    def unpack_http_header (self, header_block):
        self.decoder.feed (header_block)
        hs = header_set()
        while not self.decoder.done:
            hname, hval = self.decoder.get_header()
            hs[hname] = hval
        LOG ('headers', hs.headers)
        return hs

    def pack_http_header (self, hset):
        return self.encoder (hset)

    def send_goaway (self, last_stream_id, error_code, debug_data):
        payload = struct.pack ('>lL', last_stream_id, error_code) + debug_data
        self.send_frame (0x07, 0x00, 0x00, payload)

    def frame_rst_stream (self, flags, stream_id, payload):
        LOG ('frame_rst_stream', stream_id)
        try:
            del self.streams[stream_id]
        except KeyError:
            LOG ('bad rst_stream', stream_id)
        try:
            del self.priorities[stream_id]
        except KeyError:
            pass

    def frame_priority (self, flags, stream_id, payload):
        stream_dep, weight = struct.unpack ('<lB', payload)
        LOG ('priority', stream_dep, weight)
        if stream_id == 0:
            self.send_goaway (0, ERROR.PROTOCOL_ERROR, "priority with stream_id 0")
            self.close()
        else:
            self.priorities[stream_id] = weight

    def frame_goaway (self, flags, stream_id, payload):
        LOG ('frame_goaway', flags, stream_id, payload)
        self.close()

    def frame_data (self, flags, stream_id, payload):
        probe = self.streams.get (stream_id, None)
        if probe is not None:
            probe.file.content_fifo.push (payload)
            if flags & FLAGS.END_STREAM:
                probe.file.content_fifo.push (None)
                del self.streams[stream_id]
        else:
            self.log ('orphaned data frame [%d bytes] for stream %d\n' % (len(payload), stream_id))

    def frame_push_promise (self, flags, stream_id, payload):
        import pdb; pdb.set_trace()
    def frame_continuation (self, flags, stream_id, payload):
        import pdb; pdb.set_trace()

    def handle_request (self, req):
        try:
            handler = self.pick_handler (req)
            LOG ('handler', repr(handler))
            if handler:
                # XXX with_timeout()
                handler.handle_request (req)
            else:
                req.error (404)
        except:
            tb = coro.compact_traceback()
            req.error (500, tb)
            self.log ('error: %r request=%r tb=%r' % (self.peer, req, tb))

class h2_tlslite_server (tlslite_server):

    protocol = 'h2'

    def __init__ (self, addr, cert_path, key_path, settings=None):
        tlslite_server.__init__ (self, addr, cert_path, key_path, nextProtos=['h2', 'http/1.1'], settings=settings)

    def create_connection (self, conn, addr):
        if conn.next_proto == b'h2':
            return h2_connection (self, conn, addr)
        else:
            return connection (self, conn, addr)

class h2_openssl_server (openssl_server):

    protocol = 'h2'

    def create_connection (self, conn, addr):
        # ensure that negotiation finishes...
        selected = conn.ssl.get_alpn_selected()
        LOG ('h2_openssl_server', 'selected', selected)
        if selected == b'h2':
            return h2_connection (self, conn, addr)
        else:
            return connection (self, conn, addr)

class h2_s2n_server (s2n_server):

    protocol = 'h2'

    def create_connection (self, conn, addr):

        def unproto (n):
            from coro.ssl.s2n import PROTOCOL
            return PROTOCOL.reverse_map.get (n, "unknown")

        LOG ('h2_s2n_server', repr(conn))
        conn._check_negotiated()
        s2n = conn.s2n_conn
        LOG ('ALPN', s2n.get_application_protocol())
        if conn.s2n_conn.get_application_protocol() == b'h2':
            return h2_connection (self, conn, addr)
        else:
            return connection (self, conn, addr)

# --------------------------------------------------------------------------------
#                             h2 client
# --------------------------------------------------------------------------------

from coro.http import client as http_client

class h2_client_request (http_client.request):

    _has_body = False

    def wake (self):
        if self.rfile and self.force:
            self.content = self.rfile.read()
        self.latch.wake_all()
        if self.rfile and not self.force:
            self.rfile.wait()

    def wait (self):
        pass

    def has_body (self):
        return self._has_body

class h2_client (h2_protocol, http_client.client):

    is_server = False

    def __init__ (self, host, port=443, conn=None, inflight=100):
        self.counter = 1
        self.send_mutex = coro.mutex()
        http_client.client.__init__ (self, host, port, conn, inflight)
        # replace the fifo with a dictionary (h2 is not serialized)
        self.pending = {}

    def send_thread (self):
        self.conn.send (preface)
        h2_protocol.send_thread (self)

    def read_thread (self):
        try:
            self.read_frames()
        except coro.ClosedError as err:
            for sid, req in self.pending.iteritems():
                req.set_error (err)

    def close (self):
        self.conn.close()

    def send_frame (self, frame):
        with self.send_mutex:
            return self.conn.send (frame)

    def push_data_frame (self, stream_id, data, last):
        if last:
            flags = FLAG_FIN
        else:
            flags = 0
        self.send_frame (self.pack_data_frame (stream_id, flags, data))

    def send_request (self, method, uri, headers, content=None, force=False):
        try:
            self.inflight.acquire (1)
            req = h2_client_request (method.upper(), uri, headers, content, force)
            sid = self._send_request (method, uri, headers, content)
            self.pending[sid] = req
            LOG ('send_request', repr(req))
            return req
        finally:
            self.inflight.release (1)

    def _send_request (self, method, uri, headers, content):
        if not headers.has_key ('host'):
            headers['host'] = self.host
        if content:
            has_data = True
        else:
            has_data = False
        headers.set_one (':method', method)
        headers.set_one (':scheme', 'https')
        headers.set_one (':path', uri)
        #headers.set_one (':version', 'HTTP/1.1')
        sid = self.push_syn_stream (headers, has_data)
        if content:
            # tricky, hold one block back
            last = None
            for block in content:
                if last:
                    self.push_data_frame (sid, block, False)
                last = block
            self.push_data_frame (sid, last, True)
        return sid
"""Local upstream fixture proves terminal delivery without waiting for EOF."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import http.client
import json
import threading
import unittest

from docket.toolstack import StreamMetadata


class StreamTests(unittest.TestCase):
    def test_terminal_frame_with_upstream_socket_kept_open(self):
        release=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Type','text/event-stream')
                self.send_header('Transfer-Encoding','chunked')
                self.end_headers()
                for event in ({'type':'response.created'},
                              {'type':'response.completed','response':{'usage':{'output_tokens':7}}}):
                    raw=b'data: '+json.dumps(event).encode()+b'\n\n'
                    self.wfile.write(('%x\r\n'%len(raw)).encode()+raw+b'\r\n');self.wfile.flush()
                release.wait(2)  # deliberately no EOF/chunk terminator until client is done
                self.close_connection=True
            def log_message(self,*args): pass
        with HTTPServer(('127.0.0.1',0),Handler) as server:
            thread=threading.Thread(target=server.serve_forever);thread.start()
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=0.5)
            metadata=[];observer=StreamMetadata(metadata.append)
            try:
                connection.request('GET','/fixture')
                response=connection.getresponse()
                done=False
                for _ in range(4):
                    done=observer.feed(response.read1(8192))
                    if done: break
                self.assertTrue(done)
                self.assertFalse(release.is_set())
                self.assertEqual(metadata[-1]['type'],'response.completed')
                self.assertEqual(metadata[-1]['output_tokens'],7)
            finally:
                connection.close();release.set();server.shutdown();thread.join(timeout=3)
                self.assertFalse(thread.is_alive())

    def test_incomplete_and_failed_are_not_completed_and_no_text_audit(self):
        for kind in ('response.incomplete','response.failed'):
            audit=[];observer=StreamMetadata(audit.append)
            raw=b'data: '+json.dumps({'type':kind,'text':'DO_NOT_LOG_THIS'}).encode()+b'\n\n'
            self.assertTrue(observer.feed(raw))
            self.assertEqual(audit[-1]['type'],kind)
            self.assertNotIn('DO_NOT_LOG_THIS',json.dumps(audit))
        self.assertTrue(StreamMetadata(lambda x:None).feed(b'data: [DONE]\n\n'))

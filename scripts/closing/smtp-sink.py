"""Local SMTP sink: deliberately reject first DATA, then accept locally; never relay."""
import hashlib
import json
import socketserver
import threading
from datetime import datetime, timezone
from pathlib import Path

out = Path('.scratch/closing-evidence/tool-reliability')
out.mkdir(parents=True, exist_ok=True)
events = []
lock = threading.Lock()

class Handler(socketserver.StreamRequestHandler):
    def send(self, text):
        self.wfile.write((text + '\r\n').encode())
        self.wfile.flush()

    def handle(self):
        self.send('220 closing.local local-only SMTP sink')
        recipient = None
        while line := self.rfile.readline():
            text = line.decode(errors='replace').strip()
            command = text.split(' ', 1)[0].upper()
            if command in ('EHLO', 'HELO'):
                self.send('250 closing.local')
            elif command == 'MAIL':
                self.send('250 OK' if 'closing-sender@example.test' in text else '550 Only synthetic sender accepted')
            elif command == 'RCPT':
                recipient = 'closing-review@example.test' if 'closing-review@example.test' in text else None
                self.send('250 OK' if recipient else '550 Only synthetic local recipient accepted')
            elif command == 'DATA':
                if recipient is None:
                    self.send('503 Recipient required')
                    continue
                self.send('354 End data with <CRLF>.<CRLF>')
                body = b''
                while (chunk := self.rfile.readline()) not in (b'.\r\n', b'', b'.\n'):
                    body += chunk
                with lock:
                    rejected = not events
                    events.append({'observedAtUTC': datetime.now(timezone.utc).isoformat(),
                                   'status': 'rejected-451' if rejected else 'accepted-locally',
                                   'recipient': recipient, 'bodySHA256': hashlib.sha256(body).hexdigest(),
                                   'bytes': len(body)})
                    (out / 'smtp-events.json').write_text(json.dumps(events, indent=2) + '\n')
                    if not rejected:
                        (out / 'accepted-synthetic-message.eml').write_bytes(body)
                self.send('451 Deliberate one-time test rejection' if rejected else '250 Locally accepted; no forwarding')
            elif command == 'QUIT':
                self.send('221 Bye')
                return
            elif command in ('RSET', 'NOOP'):
                self.send('250 OK')
            else:
                self.send('502 Unsupported')

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

with Server(('0.0.0.0', 25265), Handler) as server:
    print('Local SMTP sink ready at 25265; only example.test; no relay', flush=True)
    server.serve_forever()

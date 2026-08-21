"""
Helper process for test_dispatch_stdin_isolation.py (memnos#132): opens a
fifo for writing and writes JSON-RPC-shaped lines into it forever, never
closing — the same shape as an MCP host's live stdio JSON-RPC stream, which
does not hit EOF the way a closed/redirected stdin does. Killed by the test
once it no longer needs the fifo held open.
"""
import sys
import time

path = sys.argv[1]

with open(path, "wb") as f:
    while True:
        f.write(b'{"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n')
        f.flush()
        time.sleep(0.2)

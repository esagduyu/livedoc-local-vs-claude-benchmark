#!/usr/bin/env python3
"""Test script for the livedoc server."""

import socket
import json
import threading
import time


def run_client(name, edits, port=12345):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(('127.0.0.1', port))
    
    # Join
    msg = json.dumps({'type': 'join', 'doc': 'test', 'client': name}) + '\n'
    s.send(msg.encode())
    data = b''
    while b'\n' not in data:
        data += s.recv(4096)
    print(f'{name} joined: {data.decode().strip()}')
    
    # Apply edits
    for pos, text in edits:
        op_msg = json.dumps({'type': 'op', 'doc': 'test', 'rev': 0, 'id': name + '-1', 'op': [{'ins': pos, 'text': text}]}) + '\n'
        s.send(op_msg.encode())
        data = b''
        while b'\n' not in data:
            data += s.recv(4096)
        resp = json.loads(data.decode())
        print(f'{name} op ack: {resp}')
    
    s.close()


t = threading.Thread(target=run_client, args=('Bob', [(0, 'Hello'), (5, ' World')]))
t.start()
time.sleep(0.5)

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.connect(('127.0.0.1', 12345))
msg = json.dumps({'type': 'join', 'doc': 'test', 'client': 'Alice'}) + '\n'
s.send(msg.encode())
data = b''
while b'\n' not in data:
    data += s.recv(4096)
print(f'Alice joined: {data.decode().strip()}')
s.close()
t.join(timeout=3)
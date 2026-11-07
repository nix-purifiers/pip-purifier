# SPDX-License-Identifier: MIT
"""Exercise loopback TCP/UDP and prove external networking remains denied."""
import errno
import json
import platform
import socket
import sys


def receive(stream, expected):
    actual = b''
    while len(actual) < len(expected):
        part = stream.recv(len(expected) - len(actual))
        assert part, actual
        actual += part
    assert actual == expected, actual


with socket.socket() as server, socket.socket() as client:
    server.settimeout(3)
    client.settimeout(3)
    server.bind(('127.0.0.1', 0))
    server.listen(1)
    client.connect(server.getsockname())
    connection, _ = server.accept()
    with connection:
        connection.settimeout(3)
        client.sendall(b'tcp-request')
        receive(connection, b'tcp-request')
        connection.sendall(b'tcp-response')
        receive(client, b'tcp-response')

with socket.socket(type=socket.SOCK_DGRAM) as server, socket.socket(type=socket.SOCK_DGRAM) as client:
    server.settimeout(3)
    client.settimeout(3)
    server.bind(('127.0.0.1', 0))
    client.sendto(b'udp-request', server.getsockname())
    payload, address = server.recvfrom(128)
    assert payload == b'udp-request', payload
    server.sendto(b'udp-response', address)
    payload, _ = client.recvfrom(128)
    assert payload == b'udp-response', payload

# RFC 5737 TEST-NET-2: no dependency on a live Internet service. Darwin's
# permission must reject the syscall, rather than merely time out on routing.
allowed_errors = {errno.EPERM, errno.EACCES}
if sys.platform != 'darwin':
    allowed_errors |= {errno.ENETUNREACH, errno.EHOSTUNREACH}
external = {}
for name, kind in [('tcp', socket.SOCK_STREAM), ('udp', socket.SOCK_DGRAM)]:
    with socket.socket(type=kind) as client:
        client.settimeout(3)
        try:
            if kind == socket.SOCK_STREAM:
                client.connect(('198.51.100.1', 443))
            else:
                client.sendto(b'network-denial-probe', ('198.51.100.1', 443))
        except OSError as error:
            assert error.errno in allowed_errors, (name, error)
            external[name] = {'errno': error.errno, 'name': errno.errorcode[error.errno]}
        else:
            raise AssertionError(f'external {name} networking was allowed')
print(json.dumps({'platform': sys.platform, 'machine': platform.machine(),
                  'python': sys.version, 'loopback': {'tcp': 'passed', 'udp': 'passed'},
                  'externalTestnet': external}, indent=2))

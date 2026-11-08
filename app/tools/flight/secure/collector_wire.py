"""Dedicated bounded current-state reply wire, never Agent requests.

The expanded P-private ancestor inventory is authenticated twice. Ordinary
requests/observations retain their original 4096-byte bound. This channel has
an explicit 16384-byte cap before allocating its body, with the same schemas,
HMAC, OS peer, nonce, replay and whole-transaction deadline checks.
"""
import hmac
import struct
from .spec import strict_json,SecurityError
from .policy import proof
from .ipc_transport import _read
STATE_LIMIT=16384

def unseal_state(token,raw):
    value=strict_json(raw,limit=STATE_LIMIT);signature=value.pop('proof',None)
    if type(signature) is not str or not hmac.compare_digest(proof(token,value),signature):raise SecurityError('State authentication rejected')
    return value

def receive_state_frame(stream):
    size=struct.unpack('!I',_read(stream,4))[0]
    if not 0<size<=STATE_LIMIT:raise SecurityError('State frame bound rejected')
    raw=_read(stream,size);strict_json(raw,limit=STATE_LIMIT);return raw

def send_state_frame(stream,raw):
    strict_json(raw,limit=STATE_LIMIT)
    if not 0<len(raw)<=STATE_LIMIT:raise SecurityError('State frame bound rejected')
    stream.sendall(struct.pack('!I',len(raw))+raw)

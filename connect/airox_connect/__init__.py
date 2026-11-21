"""AIROX Connect, the PC side: airox-connectd lets a paired Android phone or tablet on the same network
talk to AIRA, see what is running, use the Task Manager, send files both ways, share the clipboard
and links, act as touchpad and media remote, glance at the screen or become a second screen.

Off by default. Pairing by QR code (accepted at the PC), TLS 1.3 with the PC's certificate pinned
from the QR, a per-device token, LAN addresses only, and every action through AIRA's broker.
The protocol is in docs/CONNECT_PROTOCOL.md.
"""
__version__ = "0.1.0"
PROTOCOL = 1

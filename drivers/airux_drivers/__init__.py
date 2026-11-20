"""AIRUX Drivers: what hardware this computer has, whether each part works, and the fix.

Linux's drivers are in the kernel; what a device usually lacks is the firmware its driver loads, a
switch (rfkill), or a vendor driver (NVIDIA). This package finds out which, from sysfs and the
kernel log, never by guessing, and installs only from an allowlist through a root helper.
"""
__version__ = "0.1.0"

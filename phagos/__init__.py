"""Phagos: AIRUX's own antivirus, designed like an immune system.

Named after the phagocyte, the white blood cell that finds and eats invaders. Engines:
  rules       in-house signature rules (text, hex and regex patterns) and a SHA-256 blocklist
  heuristics  static traits of executables and scripts
  nsa         Negative Selection: detectors for "non-self" learned from this computer's own files
  clonal      Clonal Selection: detectors that caught a threat multiply and adapt to its variants
  dca         Dendritic Cell Algorithm (Danger Theory): processes judged by danger and safe signals
Standard library only, so it runs on Debian's own Python with no extra packages.
"""
__version__ = "0.1.0"

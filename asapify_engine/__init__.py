"""ASAPify risk engine: canonical data -> fuzzy MAUT rules -> per-region state machine.

Follows the backend design document: sources are canonicalised first, the rules engine picks a status with a
risk-weighted argmax, and a finite state machine records every status change for audit.
"""

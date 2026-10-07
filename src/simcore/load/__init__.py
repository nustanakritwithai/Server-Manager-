"""Load and failure tests for the simulation server.

``python -m simcore.load`` registers players through the real auth flow, sends
mixed command traffic, and checks the world after forced failures. Numbers in
the report were measured. A missing instrument is UNKNOWN or NOT INSTRUMENTED.
"""

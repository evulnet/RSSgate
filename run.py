#!/usr/bin/env python3
"""Compat shim: the real entry point is python -m rssgate
(rssgate/serve.py). Kept so old muscle memory and any external caller
keep working; the LIVE server must run as -m rssgate."""
from rssgate.serve import main

if __name__ == "__main__":
    main()

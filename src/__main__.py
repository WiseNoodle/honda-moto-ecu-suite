import sys
import os
import argparse

# Use the local eculib source bundled with HondaECU-1
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ECULIB_SOURCE = os.path.join(PROJECT_ROOT, "eculib-source")

if os.path.isdir(ECULIB_SOURCE):
    sys.path.insert(0, ECULIB_SOURCE)

from wx import App

from version import __VERSION__
from controlpanel import HondaECUControlPanel

if __name__ == '__main__':

    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('--noredirect', action='store_true', help="don't redirect stdout/stderr to gui")
    parser.add_argument('-V', '--version', action='store_true', help="show version information")
    args = parser.parse_args()

    if args.version:
        print(__VERSION__)
        sys.exit(0)

    app = App(redirect=not args.noredirect, useBestVisual=True)
    gui = HondaECUControlPanel(__VERSION__)
    app.MainLoop()
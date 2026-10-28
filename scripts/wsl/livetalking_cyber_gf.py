"""Entry point used by the start scripts: runs imood/downstream/livetalking_plugin.py."""
import os, runpy, sys

runpy.run_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "imood", "downstream", "livetalking_plugin.py"), run_name="__main__")

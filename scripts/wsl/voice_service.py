"""Entry point kept at its original path (start scripts use it): runs imood/service/main.py."""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
from imood.service.main import main

if __name__ == "__main__":
    main()

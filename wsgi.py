# WSGI entry point for hosting on PythonAnywhere (or any WSGI host).
#
# PythonAnywhere does NOT run `python server.py`; it imports a WSGI callable
# named `application`. server.py already builds the Flask app at module level
# (`app`) and only calls app.run() under `if __name__ == "__main__"`, so
# importing it here is safe and does not start the local dev server.
#
# In the PythonAnywhere web-app config, set the WSGI file to point at this,
# or paste this line into the auto-generated wsgi file:
#     from wsgi import application
#
# The secret access key is read by server.py from the BUDGET_ACCESS_KEY env
# var or a server_key.txt file next to server.py. On PythonAnywhere the
# simplest path is to upload server_key.txt (one line: the key) into the
# project folder — it's gitignored so it never lands in the public repo.

import os
import sys

# Ensure the project directory is importable regardless of how the host
# invokes this file.
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from server import app as application

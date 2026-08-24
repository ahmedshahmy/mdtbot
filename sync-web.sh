#!/bin/sh
# One-command sync of the web app into docs/ (the folder GitHub Pages serves).
# Run this after changing medical-web/, then commit and push.
rsync -a --delete "$(dirname "$0")/medical-web/" "$(dirname "$0")/docs/"
echo "docs/ synced from medical-web/"

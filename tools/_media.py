"""One definition of which files this project treats as media.

The GUI and the CLI each had their own set, and they disagreed: the CLI accepted
.gif/.ppm/.pgm images and .ts video, the GUI refused them. Both read the lists
from here now.
"""
from __future__ import annotations

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tga", ".ppm", ".pgm"}
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".ts"}
MEDIA_EXT = IMAGE_EXT | VIDEO_EXT

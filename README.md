# PixMatch

PixMatch is a modern, cross-platform duplicate-image finder inspired by VisiPics, built with PySide6. 

![Basic view of the application](https://github.com/rheard/markdown/blob/main/pixmatch/basic.jpg?raw=true)
    
PixMatch scans folders (and ZIP archives) for visually similar images, groups matches, 
    and lets you quickly keep, ignore, or delete files from a clean GUI. 
    Rotated, mirrored or recompressed imgaes are no match for PixMatch!
    PixMatch can even detect visually similar GIFs and animated WebP files.
    Files inside ZIPs are treated as read-only “sources of truth”
    —never deleted—so you can safely compare against archived libraries.


Supported extensions: `.jpg`, `.jpeg`, `.png`, `.webp`, `.tif`, `.tiff`, `.bmp`, `.gif`, `.zip`.


## Install

PixMatch is a standard Python app (GUI via PySide6).

```bash
python -m pip install pixmatch[gui]
```

## Running

```bash
python -m pixmatch
```

### Usage

Simply select some folders to parse and then click begin. 

Once duplicate groups begin to appear in the duplicates view,
    you can start to select actions for them and then execute those actions. 
Clicking on a tile will cycle through actions, with red being delete, yellow being ignore, and green being no action.

Images which are in zips and cannot be deleted will have a rar icon to denote such, 
    and they cannot be marked for deletion.

The status bar under each image shows the full path, the file size, the uncompressed file size, 
    the frames in the image if it is an animated image, the image dimensions and the last modified date.

Basic status bar example:

![Example of the status bar with a basic image loaded](https://github.com/rheard/markdown/blob/main/pixmatch/basic_status.jpg?raw=true)

Animated image status bar example:

![Example of the status bar with an animated image loaded](https://github.com/rheard/markdown/blob/main/pixmatch/gif_status.jpg?raw=true)

#### Notes
 * An exact match checkbox is provided. If strength is 10 and this checkbox is checked, 
    SHA-256 file hashes will be used instead of perceptual hashes.

#### Optional Args:
```markdown
positional arguments:
  folders     Folders to load into the selected file path display (to speed up testing).

options:
  --verbose   More detailed logging
```

## Acknowledgements

* Thanks to anyone who supported this effort, including the teams behind PySide6, Pillow, PyPI, and many other projects.
* Thanks to Johannes Buchner and the team behind imagehash, which serves as a large backbone in this application and saved me a lot of time.
* Thanks to Guillaume Fouet (aka Ozone) for VisiPics and the inspiration. Please don't be mad, I just wanted some new features like better gif and zip support.
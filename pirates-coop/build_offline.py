"""Wrap app/index.html (authored for the Artifact skeleton) into a standalone offline page.
Output: offline/index.html + offline/assets (copied). Open offline/index.html directly or serve the folder."""
import shutil, os
here = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(here, 'app/index.html'), encoding='utf-8').read()
head = ('<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n')
# move <title>/<meta>/<link>/<style> (everything before the first <div id="app">) into <head>
i = src.index('<div id="bgwrap"')
out = head + src[:i] + '</head>\n<body>\n' + src[i:] + '\n</body>\n</html>\n'
dst = os.path.join(here, 'offline')
os.makedirs(dst, exist_ok=True)
open(os.path.join(dst, 'index.html'), 'w', encoding='utf-8').write(out)
if os.path.exists(os.path.join(dst, 'assets')):
    shutil.rmtree(os.path.join(dst, 'assets'))
shutil.copytree(os.path.join(here, 'app/assets'), os.path.join(dst, 'assets'))
print('offline/index.html written')

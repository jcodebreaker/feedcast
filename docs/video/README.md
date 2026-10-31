# Explainer video

`scene.html` is a deterministic 44-second scene (`window.render(t)`); every person, post, repo and paper in it is invented. `render.mjs` drives headless Chrome (macOS path) to screenshot each frame.

```sh
node docs/video/render.mjs frames "file://$PWD/docs/video/scene.html" /tmp/feedcast-frames 30 44 1280 720 1.5
```

```sh
ffmpeg -y -framerate 30 -i /tmp/feedcast-frames/f%05d.png -c:v libx264 -preset slow -crf 22 -pix_fmt yuv420p -movflags +faststart docs/feedcast.mp4 \
  && ffmpeg -y -framerate 30 -i /tmp/feedcast-frames/f%05d.png -vf "fps=12,scale=800:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=64:stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle" docs/feedcast.gif \
  && cp /tmp/feedcast-frames/f01005.png docs/poster.png
```

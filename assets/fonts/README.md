# Captcha fonts

The image CAPTCHA looks for a TrueType font in this order:

1. `assets/fonts/DejaVuSans-Bold.ttf` (or `assets/fonts/captcha.ttf`) — drop your own here
2. System DejaVu fonts (Docker image installs `fonts-dejavu`)
3. Windows Arial (local dev)
4. Pillow's built-in bitmap font (fallback, lower quality)

To guarantee a crisp captcha everywhere, place a `.ttf` file named
`DejaVuSans-Bold.ttf` or `captcha.ttf` in this folder. DejaVu Sans is a good
free choice: https://dejavu-fonts.github.io/

The Docker image already installs `fonts-dejavu`, so no manual step is needed
for container deployments.

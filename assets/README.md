# App Icon

Place your application icon here:

- `icon.icns` - macOS icon file (required for .dmg)
- `icon.png` - Source PNG file (1024x1024px recommended)

## Creating an icon

### From PNG to ICNS (macOS)

1. Create a 1024x1024px PNG file
2. Create iconset directory:
   ```bash
   mkdir icon.iconset
   ```

3. Generate different sizes:
   ```bash
   sips -z 16 16     icon.png --out icon.iconset/icon_16x16.png
   sips -z 32 32     icon.png --out icon.iconset/icon_16x16@2x.png
   sips -z 32 32     icon.png --out icon.iconset/icon_32x32.png
   sips -z 64 64     icon.png --out icon.iconset/icon_32x32@2x.png
   sips -z 128 128   icon.png --out icon.iconset/icon_128x128.png
   sips -z 256 256   icon.png --out icon.iconset/icon_128x128@2x.png
   sips -z 256 256   icon.png --out icon.iconset/icon_256x256.png
   sips -z 512 512   icon.png --out icon.iconset/icon_256x256@2x.png
   sips -z 512 512   icon.png --out icon.iconset/icon_512x512.png
   sips -z 1024 1024 icon.png --out icon.iconset/icon_512x512@2x.png
   ```

4. Convert to ICNS:
   ```bash
   iconutil -c icns icon.iconset
   ```

### Quick Placeholder

For development, the app works without an icon — the Dock just shows the default Python/Qt icon.

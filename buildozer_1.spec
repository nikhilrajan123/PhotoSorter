[app]
title = PhotoSorter Pro
package.name = photosorter
package.domain = org.photosorter
source.dir = .
source.include_exts = py,png,jpg,kv,atlas
version = 1.0

# Python requirements — all the libraries your app needs
requirements = python3,kivy==2.3.0,pillow,pillow-heif,requests,piexif

# Android orientation
orientation = portrait

# Android permissions
android.permissions = READ_EXTERNAL_STORAGE,WRITE_EXTERNAL_STORAGE,MANAGE_EXTERNAL_STORAGE,INTERNET

# Target Android version
android.api = 33
android.minapi = 26
android.ndk = 25b
android.sdk = 33

# Architecture (arm64 covers all modern Android phones including Samsung S24)
android.archs = arm64-v8a

# Allow writing to SD card / external storage
android.allow_backup = True

[buildozer]
log_level = 2
warn_on_root = 1

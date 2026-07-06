#!/usr/bin/env python3
# -*- coding: UTF-8 -*-
# build_addon.py - Build the Hermes Accessibility NVDA addon package

import os
import zipfile
import json

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ADDON_DIR = os.path.join(SCRIPT_DIR, "addon")
OUTPUT_DIR = SCRIPT_DIR

# Read version from buildVars
sys_path_backup = None
buildVars = {}
with open(os.path.join(SCRIPT_DIR, "buildVars.py")) as f:
    code = f.read()
exec(compile(code, "buildVars.py", "exec"), buildVars)

addon_info = buildVars.get("addon_info", {})
addon_name = addon_info.get("addon_name", "hermesAccessibility")
version = addon_info.get("addon_version", "1.0.0")
output_filename = "%s-%s.nvda-addon" % (addon_name, version)
output_path = os.path.join(OUTPUT_DIR, output_filename)

print("=" * 60)
print("Building Hermes Accessibility NVDA Addon")
print("  Name:    %s" % addon_name)
print("  Version: %s" % version)
print("  Output:  %s" % output_path)
print("=" * 60)

# Files to include from the addon directory
# Standard NVDA addon structure:
#   manifest.ini              -> root of zip
#   appModules/hermes.py      -> appModules/ in zip
#   globalPlugins/hermesMessageNav.py -> globalPlugins/ in zip
#   readme.html               -> in doc/en/ or at root

with zipfile.ZipFile(output_path, 'w', zipfile.ZIP_DEFLATED) as zf:
	# Walk the addon directory structure
	for root, dirs, files in os.walk(ADDON_DIR):
		# Skip __pycache__ and other generated dirs
		dirs[:] = [d for d in dirs if not d.startswith('__') and d != '.git']
		for f in files:
			if f.endswith('.pyc'):
				continue
			file_path = os.path.join(root, f)
			arc_name = os.path.relpath(file_path, ADDON_DIR).replace('\\', '/')
			zf.write(file_path, arc_name)
			print("  + %s" % arc_name)

	# Include readme at the root
	readme_path = os.path.join(SCRIPT_DIR, "readme.html")
	if os.path.exists(readme_path):
		zf.write(readme_path, "readme.html")
		print("  + readme.html")

	# Include the app.asar auto-patch script
	patch_script = os.path.join(SCRIPT_DIR, "patch_app_asar.js")
	if os.path.exists(patch_script):
		zf.write(patch_script, "patch_app_asar.js")
		print("  + patch_app_asar.js")

	# Include license
	lic_path = os.path.join(SCRIPT_DIR, "COPYING")
	if not os.path.exists(lic_path):
		# Write a simple license
		with open(lic_path, 'w') as lf:
			lf.write("""This addon is free software. You can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or
(at your option) any later version.
""")
		zf.write(lic_path, "COPYING")
		print("  + COPYING")
	else:
		zf.write(lic_path, "COPYING")
		print("  + COPYING")

# Verify the package
print()
print("Verifying package...")
with zipfile.ZipFile(output_path, 'r') as zf:
	names = zf.namelist()
	print("  Contents:")
	for n in sorted(names):
		info = zf.getinfo(n)
		print("    %-40s %8d bytes" % (n, info.file_size))

	# Check manifest is readable
	if "manifest.ini" in names:
		manifest_data = zf.read("manifest.ini").decode('utf-8')
		print()
		print("  manifest.ini preview:")
		for line in manifest_data.split('\n')[:5]:
			if line.strip():
				print("    %s" % line)

file_size = os.path.getsize(output_path)
print()
print("Build successful!")
print("  Package: %s" % output_path)
print("  Size:    %d bytes (%.1f KB)" % (file_size, file_size / 1024.0))
print()
print("To install:")
print("  1. Open NVDA menu (NVDA+N)")
print("  2. Go to Tools > Manage Add-ons")
print("  3. Click Install")
print("  4. Select: %s" % output_path)

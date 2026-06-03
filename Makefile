# SlopBro
# by throwaway96
# https://github.com/throwaway96/slopbro
# Copyright 2026. Licensed under AGPL v3 or later. No warranties.

INPUT_SCRIPT = slopbro.py
WWWROOT_FILES = index.html autoroot.sh package.json main.js
PACKED_FILE = dist/slopbro_packed.py

PACKAGING_TOOL = tools/package_single_file.py

.PHONY: all
all: dist

.PHONY: dist
dist: $(PACKAGING_TOOL) $(INPUT_SCRIPT) $(addprefix wwwroot/, $(WWWROOT_FILES))
	python3 '$(PACKAGING_TOOL)' --source '$(INPUT_SCRIPT)' --files $(WWWROOT_FILES) --out '$(PACKED_FILE)'

.PHONY: clean
clean:
	rm -rf -- 'dist/'

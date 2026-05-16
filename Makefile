CC      ?= gcc
CFLAGS  ?= -O2 -Wall
PREFIX  ?= /usr/local
DESTDIR ?=

all: clevo-fan-cli

clevo-fan-cli: clevo-fan-cli.c
	$(CC) $(CFLAGS) -o $@ $<

# Default install: deploys CLI + BOTH unit options (legacy 5-min timer
# AND the new load-aware daemon). User picks which to enable.
install: install-cli install-daemon install-legacy
	@echo
	@echo "Installed. Pick ONE of the following to enable:"
	@echo
	@echo "  RECOMMENDED  - load-aware daemon (needs nvidia-smi):"
	@echo "    sudo systemctl daemon-reload"
	@echo "    sudo systemctl enable --now clevo-fan-daemon.service"
	@echo
	@echo "  LEGACY  - 5-min force-max timer (works on any chassis,"
	@echo "  no nvidia-smi needed; sets max regardless of load):"
	@echo "    sudo systemctl daemon-reload"
	@echo "    sudo systemctl enable --now clevo-fan-max.timer"

# CLI binary (used by both unit options).
install-cli: clevo-fan-cli
	install -d $(DESTDIR)$(PREFIX)/bin
	install -m 4755 -o root -g root clevo-fan-cli $(DESTDIR)$(PREFIX)/bin/clevo-fan-cli

# Load-aware daemon (Python) + its unit file.
install-daemon: install-cli
	install -d $(DESTDIR)$(PREFIX)/bin
	install -m 755 clevo_fan_daemon.py $(DESTDIR)$(PREFIX)/bin/clevo_fan_daemon.py
	install -d $(DESTDIR)/etc/systemd/system
	install -m 0644 systemd/clevo-fan-daemon.service $(DESTDIR)/etc/systemd/system/

# Legacy 5-min force-max timer + oneshot service.
install-legacy:
	install -d $(DESTDIR)/etc/systemd/system
	install -m 0644 systemd/clevo-fan-max.service $(DESTDIR)/etc/systemd/system/
	install -m 0644 systemd/clevo-fan-max.timer   $(DESTDIR)/etc/systemd/system/

uninstall:
	systemctl disable --now clevo-fan-daemon.service 2>/dev/null || true
	systemctl disable --now clevo-fan-max.timer      2>/dev/null || true
	rm -f $(DESTDIR)$(PREFIX)/bin/clevo-fan-cli
	rm -f $(DESTDIR)$(PREFIX)/bin/clevo_fan_daemon.py
	rm -f $(DESTDIR)/etc/systemd/system/clevo-fan-daemon.service
	rm -f $(DESTDIR)/etc/systemd/system/clevo-fan-max.service
	rm -f $(DESTDIR)/etc/systemd/system/clevo-fan-max.timer
	systemctl daemon-reload 2>/dev/null || true

clean:
	rm -f clevo-fan-cli

.PHONY: all install install-cli install-daemon install-legacy uninstall clean

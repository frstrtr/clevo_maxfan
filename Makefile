CC      ?= gcc
CFLAGS  ?= -O2 -Wall
PREFIX  ?= /usr/local
DESTDIR ?=

all: clevo-fan-cli

clevo-fan-cli: clevo-fan-cli.c
	$(CC) $(CFLAGS) -o $@ $<

install: clevo-fan-cli
	install -d $(DESTDIR)$(PREFIX)/bin
	install -m 4755 -o root -g root clevo-fan-cli $(DESTDIR)$(PREFIX)/bin/clevo-fan-cli
	install -d $(DESTDIR)/etc/systemd/system
	install -m 0644 systemd/clevo-fan-max.service $(DESTDIR)/etc/systemd/system/
	install -m 0644 systemd/clevo-fan-max.timer   $(DESTDIR)/etc/systemd/system/
	@echo
	@echo "Installed. To enable persistent max-fan, run:"
	@echo "  sudo systemctl daemon-reload"
	@echo "  sudo systemctl enable --now clevo-fan-max.timer"

uninstall:
	systemctl disable --now clevo-fan-max.timer 2>/dev/null || true
	rm -f $(DESTDIR)$(PREFIX)/bin/clevo-fan-cli
	rm -f $(DESTDIR)/etc/systemd/system/clevo-fan-max.service
	rm -f $(DESTDIR)/etc/systemd/system/clevo-fan-max.timer
	systemctl daemon-reload 2>/dev/null || true

clean:
	rm -f clevo-fan-cli

.PHONY: all install uninstall clean

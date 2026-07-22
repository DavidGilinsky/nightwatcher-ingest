# nightwatcher-ingest — source install / Debian package
# Debian/Ubuntu: prefer `make deb` + `apt install ./nightwatcher-ingest_*.deb`.
PREFIX  ?= /usr
DESTDIR ?=

.PHONY: install uninstall deb clean

install:
	install -Dm0755 nwingest.py $(DESTDIR)$(PREFIX)/bin/nwingest
	install -Dm0644 systemd/nwingest.service $(DESTDIR)/lib/systemd/system/nwingest.service
	@if [ ! -f $(DESTDIR)/etc/nwingest/nwingest.yaml ]; then \
	    install -Dm0644 nwingest.example.yaml $(DESTDIR)/etc/nwingest/nwingest.yaml; \
	else \
	    echo "keeping existing $(DESTDIR)/etc/nwingest/nwingest.yaml"; \
	fi
	@echo "Installed. Edit /etc/nwingest/nwingest.yaml, then: systemctl enable --now nwingest"

uninstall:
	rm -f $(DESTDIR)$(PREFIX)/bin/nwingest
	rm -f $(DESTDIR)/lib/systemd/system/nwingest.service
	@echo "Left /etc/nwingest/ in place (remove it manually if you want)."

deb:
	sh packaging/build-deb.sh

clean:
	rm -f nightwatcher-ingest_*_all.deb

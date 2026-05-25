## Cyprus 2026 TikTok Monitor — local dev helpers
##
## Usage:
##   make deploy   — strip + stage + commit + push (safe manual publish)
##   make strip    — clean non-public rows from politician_ads_public.db
##   make check    — classify disappeared ads (enforcement vs voluntary)
##
## Always use `make deploy` instead of bare `git push` when manually
## updating politician_ads_public.db — the strip step ensures no
## content_keyword / FP / numeric-handle rows leak to the dashboard
## (omitting it caused the 483-row pollution on 2026-05-20 and the
## 852-vs-885 mismatch on 2026-05-25).

.PHONY: deploy strip check

# Strip non-public rows, stage DB, commit with a sensible message, push.
deploy: strip
	@echo "→ Staging DB..."
	git add politician_ads_public.db
	@if git diff --cached --quiet; then \
		echo "  Nothing changed — skipping commit."; \
	else \
		git commit -m "chore: manual DB update $$(date -u +%Y-%m-%dT%H:%MZ)"; \
		git push; \
		echo "  ✓ Pushed. Streamlit will redeploy in ~2 min."; \
	fi

# Run strip in-place on the public DB.
strip:
	@echo "→ Running strip_public_db.py..."
	POLITICIAN_ADS_DB=politician_ads_public.db python strip_public_db.py

# Classify disappeared ads (enforcement vs voluntary) via headless browser.
check:
	@echo "→ Running check_ad_library_status.py..."
	python check_ad_library_status.py $(ARGS)

# Apple clients build entry points. Everything here runs on a Mac with
# Xcode 16+; `make test` also works on Linux with a Swift toolchain.

XCODEGEN ?= xcodegen
LIBTAILSCALE_REPO ?= https://github.com/tailscale/libtailscale

.PHONY: project project-embedded tailscale test clean

## Generate Gravedecay.xcodeproj (no TailscaleKit — Tailscale VPN app mode).
project:
	$(XCODEGEN) --spec project.yml

## Generate the project with the in-app tailnet node embedded.
project-embedded: Vendor/TailscaleKit.xcframework
	$(XCODEGEN) --spec project-embedded.yml

## Build TailscaleKit frameworks from source (needs Go + Xcode).
## Product paths inside libtailscale may drift between revisions — if the
## copy step fails, look under vendor/libtailscale/swift/build and adjust.
tailscale Vendor/TailscaleKit.xcframework: vendor/libtailscale
	$(MAKE) -C vendor/libtailscale/swift ios-fat
	$(MAKE) -C vendor/libtailscale/swift macos
	mkdir -p Vendor/macos
	find vendor/libtailscale/swift/build -name 'TailscaleKit.xcframework' \
	  -exec cp -R {} Vendor/ \; -quit
	find vendor/libtailscale/swift/build -name 'TailscaleKit.framework' \
	  -not -path '*iphone*' -not -path '*xcframework*' \
	  -exec cp -R {} Vendor/macos/ \; -quit

vendor/libtailscale:
	git clone --depth 1 $(LIBTAILSCALE_REPO) vendor/libtailscale

## Run the platform-independent core tests (works on macOS and Linux).
test:
	swift test --package-path GravedecayKit

clean:
	rm -rf Gravedecay.xcodeproj App/Generated Vendor vendor \
	  GravedecayKit/.build

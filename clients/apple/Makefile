# Apple clients build entry points. Everything here runs on a Mac with
# Xcode 16+; `make test` also works on Linux with a Swift toolchain.

XCODEGEN ?= xcodegen
SIGNING ?= CODE_SIGNING_ALLOWED=NO CODE_SIGN_IDENTITY=''
DERIVED_DATA ?= build/DerivedData

.PHONY: project app-icon test package-dmg clean

## Generate Gravedecay.xcodeproj.
project:
	$(XCODEGEN) --spec project.yml

## Regenerate every declared macOS AppIcon raster from its transparent SVG master.
app-icon:
	scripts/generate-app-icon.sh

## Run the platform-independent core tests (works on macOS and Linux).
test:
	swift test --package-path GravedecayKit

## Build a Universal 2 Release app, then a mountable DMG.  Set BUILD_DIR to
## keep Xcode output out of DerivedData; NOTARY_PROFILE optionally notarizes.
package-dmg: project
	xcodebuild -project Gravedecay.xcodeproj -scheme Gravedecay-macOS -configuration Release build -derivedDataPath "$(DERIVED_DATA)" $(SIGNING) ARCHS="arm64 x86_64" ONLY_ACTIVE_ARCH=NO -skipPackagePluginValidation
	APP="$(CURDIR)/$(DERIVED_DATA)/Build/Products/Release/Gravedecay.app" scripts/package-macos-dmg.sh

clean:
	rm -rf Gravedecay.xcodeproj App/Generated GravedecayKit/.build

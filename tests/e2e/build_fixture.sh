#!/usr/bin/env bash
# Build the fixture gem and its Marshal indexes with the real Ruby toolchain.
# Usage: tests/e2e/build_fixture.sh <output dir>
set -euo pipefail
out="${1:?output directory required}"
mkdir -p "$out/lib" && cd "$out"
cat > hellohub.gemspec <<'SPEC'
Gem::Specification.new do |s|
  s.name = "hellohub"
  s.version = "0.3.1"
  s.summary = "Fixture gem for gemhub end-to-end test"
  s.authors = ["Edi"]
  s.files = ["lib/hellohub.rb"]
  s.license = "MIT"
  s.required_ruby_version = ">= 2.5.0"
end
SPEC
echo 'module Hellohub; VERSION = "0.3.1"; end' > lib/hellohub.rb
gem build hellohub.gemspec >/dev/null
ruby -rzlib -rrubygems -e '
spec = Gem::Specification.load("hellohub.gemspec")
File.binwrite("hellohub-0.3.1.gemspec.rz", Gem.deflate(Marshal.dump(spec)))
idx = Marshal.dump([["hellohub", Gem::Version.new("0.3.1"), "ruby"]])
%w[specs latest_specs].each { |n| File.binwrite("#{n}.4.8.gz", Zlib.gzip(idx)) }
File.binwrite("prerelease_specs.4.8.gz", Zlib.gzip(Marshal.dump([])))
'
echo "fixture ready in $out"

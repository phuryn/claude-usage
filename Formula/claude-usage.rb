class ClaudeUsage < Formula
  desc "Token, cost, and session dashboard for Claude Code usage"
  homepage "https://github.com/phuryn/claude-usage"
  # Pinned to the PREVIOUS release's tag tarball, never this formula's own
  # release: the formula ships inside the repo it installs, so a self-pointing
  # sha256 would be uncomputable (the tarball would contain this very hash).
  # It therefore tracks one release behind by design — bump to the prior tag
  # each release. See AGENTS.md "Homebrew formula and self-referential SHA".
  url "https://github.com/phuryn/claude-usage/archive/refs/tags/v1.5.5.tar.gz"
  version "1.5.5"
  sha256 "d56763c4742a0903cdf1e465a8b8e7e03ae6283c7d22a86e8729eee77a39c315"
  license "MIT"
  head "https://github.com/phuryn/claude-usage.git", branch: "main"

  depends_on "python@3.13"

  def install
    libexec.install "cli.py", "scanner.py", "dashboard.py"

    # Reference the versioned interpreter (python3.13): modern python@3.x kegs
    # only ship "python3.13" in their bin — the unversioned "python3" symlink
    # lives in libexec/bin, so opt_bin/"python3" doesn't exist and the shim
    # fails at runtime with "No such file or directory" (#46).
    (bin/"claude-usage").write <<~EOS
      #!/bin/bash
      exec "#{Formula["python@3.13"].opt_bin}/python3.13" "#{libexec}/cli.py" "$@"
    EOS
    chmod 0755, bin/"claude-usage"
  end

  test do
    # 1. No-args invocation prints the usage banner — exercises the shim.
    output = shell_output("#{bin}/claude-usage")
    assert_match "Claude Code Usage Dashboard", output
    assert_match "scan", output
    assert_match "dashboard", output

    # 2. `scan` against an empty projects dir exercises the real code path
    #    end-to-end (sqlite open, glob walk, summary print) without touching
    #    the user's real ~/.claude/usage.db. Homebrew's test sandbox provides
    #    testpath, so this stays isolated.
    (testpath/"projects").mkpath
    scan_output = shell_output("#{bin}/claude-usage scan --projects-dir #{testpath}/projects")
    assert_match "Scan complete", scan_output
  end
end

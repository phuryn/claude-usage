class ClaudeUsage < Formula
  desc "Token, cost, and session dashboard for Claude Code usage"
  homepage "https://github.com/phuryn/claude-usage"
  url "https://github.com/phuryn/claude-usage/archive/af507cd42447eafcb44ac6ae31b25fae1dbbc8a6.tar.gz"
  version "0.1.0"
  sha256 "4554728852f58b254a75a317d6d99f6b9cc7ac4c2f60c21590b41abd2aa88467"
  license "MIT"
  head "https://github.com/phuryn/claude-usage.git", branch: "main"

  depends_on "python@3.13"

  def install
    libexec.install "cli.py", "scanner.py", "dashboard.py"

    (bin/"claude-usage").write <<~EOS
      #!/bin/bash
      exec "#{Formula["python@3.13"].opt_bin}/python3" "#{libexec}/cli.py" "$@"
    EOS
    chmod 0755, bin/"claude-usage"
  end

  test do
    output = shell_output("#{bin}/claude-usage")
    assert_match "Claude Code Usage Dashboard", output
    assert_match "scan", output
    assert_match "dashboard", output
  end
end

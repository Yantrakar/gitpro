// Pure helpers, no vscode import, so they can be tested with plain node.

export interface Setting {
  value: string;
  origin: string; // e.g. "file:/home/me/.gitconfig-work", "" if unset
}

export interface Status {
  text: string;
  warning: boolean;
  tooltip: string;
}

/** Parse the output of `git config --show-origin --get KEY`. */
export function parseOrigin(out: string): Setting {
  const line = out.replace(/\r?\n$/, "");
  const tab = line.indexOf("\t");
  return tab < 0 ? { value: "", origin: "" } : { origin: line.slice(0, tab), value: line.slice(tab + 1) };
}

export function status(name: Setting, email: Setting, remotes: string[], inContainer: boolean): Status {
  const where = remotes.length ? remotes.map((r) => `- ${r}`).join("\n") : "- (no remotes)";
  if (!email.value) {
    const fix = inContainer
      ? "Inside a dev container: on the host, run `gitpro pin` in this repo."
      : "Add an identity for this remote to your gitpro config, or a name and email under [default].";
    return {
      text: "$(warning) no git identity",
      warning: true,
      tooltip: `git has no user.email here and will refuse to commit.\n\n${fix}\n\nRemotes:\n${where}`,
    };
  }
  const from = (s: Setting) => (s.origin ? `\`${s.origin.replace(/^file:/, "")}\`` : "(unset)");
  return {
    text: `$(account) ${email.value}`,
    warning: false,
    tooltip: `**${name.value || "(no name)"}** <${email.value}>\n\n` +
      `email from ${from(email)}\n\nRemotes:\n${where}`,
  };
}

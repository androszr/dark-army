"use strict";
(() => {
  const defaults = window.SignalDefaults;
  const sections = {
    colors: Object.keys(defaults.colors),
    spacing: Object.keys(defaults.spacing),
    radii: Object.keys(defaults.radii),
    type: Object.keys(defaults.type),
    size: Object.keys(defaults.size),
  };
  const controls = document.getElementById("token-controls");
  const output = document.getElementById("export-json");
  const message = document.getElementById("message");
  let draft = structuredClone(defaults);

  function luminance(hex) {
    const channels = [1, 3, 5].map(i => parseInt(hex.slice(i, i + 2), 16) / 255);
    const linear = channels.map(c => c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4);
    return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
  }

  function contrast(a, b) {
    const [high, low] = [luminance(a), luminance(b)].sort((x, y) => y - x);
    return (high + 0.05) / (low + 0.05);
  }

  function valid(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw Error("Import must be a token object.");
    if (Object.keys(raw).sort().join() !== Object.keys(defaults).sort().join()
        || raw.name !== "Signal" || raw.version !== 1) throw Error("Use a Signal version 1 export.");
    for (const [section, names] of Object.entries(sections)) {
      if (!raw[section] || typeof raw[section] !== "object" || Array.isArray(raw[section])
          || Object.keys(raw[section]).sort().join() !== names.slice().sort().join()) {
        throw Error(`The ${section} section has missing or unknown tokens.`);
      }
      for (const name of names) {
        const value = raw[section][name];
        if (section === "colors" && (typeof value !== "string" || !/^#[0-9a-fA-F]{6}$/.test(value))) {
          throw Error(`${name} must be a six-digit hex color.`);
        }
        if (section !== "colors" && (!Number.isInteger(value) || value < 1 || value > 80)) {
          throw Error(`${name} must be a whole number from 1 to 80.`);
        }
      }
    }
    if (raw.size.phoneTarget < 44 || raw.size.phoneControl < 44) throw Error("Phone targets must be at least 44 points.");
    const pairs = [
      ["text", "canvas"], ["text", "surface"], ["text", "raised"],
      ["muted", "canvas"], ["muted", "surface"], ["muted", "raised"],
      ["accent", "canvas"], ["accent", "raised"],
      ["attention", "surface"], ["attention", "raised"],
      ["danger", "surface"], ["danger", "raised"], ["accentInk", "accent"],
    ];
    for (const [foreground, background] of pairs) {
      if (contrast(raw.colors[foreground], raw.colors[background]) < 4.5) {
        throw Error(`${foreground} on ${background} needs 4.5:1 text contrast.`);
      }
    }
    return raw;
  }

  function paint() {
    for (const [section, names] of Object.entries(sections)) {
      for (const name of names) {
        const value = draft[section][name];
        document.documentElement.style.setProperty(`--signal-${section === "colors" ? "" : `${section}-`}${name}`, section === "colors" ? value : `${value}px`);
      }
    }
    output.value = JSON.stringify(draft, null, 2) + "\n";
    output.scrollTop = 0;
  }

  function drawControls() {
    controls.replaceChildren();
    for (const [section, names] of Object.entries(sections)) {
      for (const name of names) {
        const wrapper = document.createElement("div");
        wrapper.className = "token-control";
        const id = `token-${section}-${name}`;
        const label = document.createElement("label");
        label.htmlFor = id;
        label.textContent = name.replace(/([A-Z])/g, " $1").replace(/^./, c => c.toUpperCase());
        const input = document.createElement("input");
        input.id = id;
        input.type = section === "colors" ? "color" : "number";
        if (section !== "colors") { input.min = "1"; input.max = "80"; input.step = "1"; }
        input.value = draft[section][name];
        input.addEventListener("input", () => {
          const next = section === "colors" ? input.value.toUpperCase() : Number(input.value);
          const candidate = structuredClone(draft);
          candidate[section][name] = next;
          try { valid(candidate); draft = candidate; paint(); message.textContent = "Preview draft updated."; }
          catch (error) { message.textContent = error.message; }
        });
        const kind = document.createElement("small");
        kind.textContent = section;
        wrapper.append(label, input, kind);
        controls.append(wrapper);
      }
    }
  }

  document.getElementById("reset").addEventListener("click", () => {
    draft = structuredClone(defaults); drawControls(); paint(); message.textContent = "Preview reset to bundled Signal tokens.";
  });
  document.getElementById("import-file").addEventListener("change", async event => {
    const file = event.target.files[0];
    if (!file) return;
    try {
      if (file.size > 32768) throw Error("Import is too large (32 KB maximum).");
      draft = valid(JSON.parse(await file.text()));
      drawControls(); paint(); message.textContent = "Valid token file imported into this preview.";
    } catch (error) { message.textContent = `Import refused: ${error.message}`; }
    event.target.value = "";
  });
  document.getElementById("export").addEventListener("click", async () => {
    paint(); output.focus(); output.select();
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) await navigator.clipboard.writeText(output.value);
      else if (!document.execCommand("copy")) throw Error("Copy unavailable");
      message.textContent = "Validated JSON copied. Update the canonical file and rebuild to change native screens.";
    } catch (_) { message.textContent = "Export selected. Copy the text above to save your tokens."; }
  });
  drawControls(); paint();
})();

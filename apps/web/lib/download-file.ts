export function downloadFile(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  try {
    const anchor = window.document.createElement("a");
    anchor.href = url;
    anchor.download = name.replace(/[^a-zA-Z0-9._-]/g, "-");
    window.document.body.appendChild(anchor);
    try { anchor.click(); } finally { anchor.remove(); }
  } finally {
    // Give the browser's download dispatch a turn before releasing the data.
    window.setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}

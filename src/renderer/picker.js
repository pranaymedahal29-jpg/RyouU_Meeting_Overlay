
const selection = document.getElementById("selection");

let startX = 0;
let startY = 0;
let dragging = false;

function updateSelection(x, y) {
  const left = Math.min(startX, x);
  const top = Math.min(startY, y);
  const width = Math.abs(x - startX);
  const height = Math.abs(y - startY);

  selection.style.display = "block";
  selection.style.left = `${left}px`;
  selection.style.top = `${top}px`;
  selection.style.width = `${width}px`;
  selection.style.height = `${height}px`;
}

document.addEventListener("mousedown", (event) => {
  if (event.button !== 0) return;

  dragging = true;
  startX = event.clientX;
  startY = event.clientY;

  updateSelection(startX, startY);
});

document.addEventListener("mousemove", (event) => {
  if (!dragging) return;

  updateSelection(event.clientX, event.clientY);
});

document.addEventListener("mouseup", async (event) => {
  if (!dragging) return;

  dragging = false;

  const left = Math.min(startX, event.clientX);
  const top = Math.min(startY, event.clientY);
  const width = Math.abs(event.clientX - startX);
  const height = Math.abs(event.clientY - startY);

  if (width < 40 || height < 40) {
    selection.style.display = "none";
    return;
  }

  const result = await window.ryouu.selectCaptureRegion({
    x: left,
    y: top,
    width,
    height
  });

  if (!result?.success) {
    selection.style.display = "none";
    alert(result?.error || "Could not select that region. Try again.");
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    window.ryouu.cancelCaptureRegion();
  }
});

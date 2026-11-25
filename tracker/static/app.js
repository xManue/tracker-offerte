"use strict";

function drawChart(canvas) {
  const values = JSON.parse(canvas.dataset.values).map(value => value === null ? null : Number(value));
  const usable = values.filter(Number.isFinite);
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (width < 40 || height < 40) return;
  const scale = window.devicePixelRatio || 1;
  canvas.width = width * scale;
  canvas.height = height * scale;
  const ctx = canvas.getContext("2d");
  ctx.scale(scale, scale);
  ctx.font = "12px system-ui";
  ctx.fillStyle = "#536560";
  if (usable.length < 2) {
    ctx.fillText("Storico insufficiente per il grafico", 8, height / 2);
    return;
  }

  const low = Math.min(...usable);
  const high = Math.max(...usable);
  const padding = { left: 57, right: 13, top: 12, bottom: 17 };
  const drawWidth = width - padding.left - padding.right;
  const drawHeight = height - padding.top - padding.bottom;
  const euro = new Intl.NumberFormat("it-IT", { maximumFractionDigits: 0 });
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let index = 0; index <= 2; index += 1) {
    const y = padding.top + index * drawHeight / 2;
    ctx.strokeStyle = "#dce5dd";
    ctx.beginPath();
    ctx.moveTo(padding.left, y);
    ctx.lineTo(width - padding.right, y);
    ctx.stroke();
    if (low !== high || index === 1) ctx.fillText(euro.format(high - index * (high - low) / 2), padding.left - 6, y);
  }
  ctx.strokeStyle = "#197856";
  ctx.lineWidth = 2;
  ctx.beginPath();
  let continuing = false;
  const points = [];
  values.forEach((value, index) => {
    if (!Number.isFinite(value)) {
      continuing = false;
      return;
    }
    const x = padding.left + drawWidth * index / (values.length - 1);
    const y = padding.top + (high === low ? 0.5 : (high - value) / (high - low)) * drawHeight;
    if (continuing) ctx.lineTo(x, y);
    else ctx.moveTo(x, y);
    points.push([x, y]);
    continuing = true;
  });
  ctx.stroke();
  ctx.fillStyle = "#197856";
  points.forEach(([x, y]) => {
    ctx.beginPath();
    ctx.arc(x, y, 2.7, 0, 2 * Math.PI);
    ctx.fill();
  });
}

document.querySelectorAll("canvas[data-values]").forEach(canvas => {
  drawChart(canvas);
  const observer = new ResizeObserver(() => drawChart(canvas));
  observer.observe(canvas);
});

/**
 * Генерирует макет для печати.
 * @param {string} singlePhotoUrl - Blob URL готового одиночного фото
 * @param {Object} config - Конфигурация макета
 * @param {number} targetDpi - Разрешение (по умолчанию 300)
 */
export const generatePrintLayout = async (
  singlePhotoUrl, 
  config = { paperW: 100, paperH: 150, photoW: 35, photoH: 45, cols: 2, rows: 3 }, 
  targetDpi = 300
) => {
  const mmToPx = targetDpi / 25.4;
  const canvasW = Math.round(config.paperW * mmToPx); 
  const canvasH = Math.round(config.paperH * mmToPx); 
  
  const photoW = Math.round(config.photoW * mmToPx);
  const photoH = Math.round(config.photoH * mmToPx);

  const canvas = document.createElement('canvas');
  canvas.width = canvasW;
  canvas.height = canvasH;
  const ctx = canvas.getContext('2d');
  
  // Белый фон листа
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, canvasW, canvasH);

  const img = new Image();
  img.src = singlePhotoUrl;
  await new Promise((resolve, reject) => {
    img.onload = resolve;
    img.onerror = reject;
  });

  // Вычисляем отступы для равномерного распределения по листу
  const gapX = (canvasW - (config.cols * photoW)) / (config.cols + 1);
  const gapY = (canvasH - (config.rows * photoH)) / (config.rows + 1);

  // Параметры уголков
  const cornerLenMm = 3; // Длина линии уголка в мм
  const cornerLenPx = Math.round(cornerLenMm * mmToPx);
  ctx.strokeStyle = '#e5e7eb'; // Светло-серый
  ctx.lineWidth = 1; // Уголки должны быть очень тонкими

  for (let row = 0; row < config.rows; row++) {
    for (let col = 0; col < config.cols; col++) {
      const x = Math.round(gapX + col * (photoW + gapX));
      const y = Math.round(gapY + row * (photoH + gapY));
      
      // 1. Рисуем само фото
      ctx.drawImage(img, x, y, photoW, photoH);

      // 2. Рисуем уголки реза, "вывернутые наружу"
      // Внутренний угол касается угла фото, линии уходят в белое поле
      
      // Верхний левый
      ctx.beginPath();
      ctx.moveTo(x - cornerLenPx, y); // Наружу влево
      ctx.lineTo(x, y); // Касается угла
      ctx.lineTo(x, y - cornerLenPx); // Наружу вверх
      ctx.stroke();

      // Верхний правый
      ctx.beginPath();
      ctx.moveTo(x + photoW, y - cornerLenPx); // Наружу вверх
      ctx.lineTo(x + photoW, y); // Касается угла
      ctx.lineTo(x + photoW + cornerLenPx, y); // Наружу вправо
      ctx.stroke();

      // Нижний левый
      ctx.beginPath();
      ctx.moveTo(x - cornerLenPx, y + photoH); // Наружу влево
      ctx.lineTo(x, y + photoH); // Касается угла
      ctx.lineTo(x, y + photoH + cornerLenPx); // Наружу вниз
      ctx.stroke();

      // Нижний правый
      ctx.beginPath();
      ctx.moveTo(x + photoW + cornerLenPx, y + photoH); // Наружу вправо
      ctx.lineTo(x + photoW, y + photoH); // Касается угла
      ctx.lineTo(x + photoW, y + photoH + cornerLenPx); // Наружу вниз
      ctx.stroke();
    }
  }

  return new Promise(resolve => {
    // Возвращаем JPG максимального качества
    canvas.toBlob(blob => resolve(blob ? URL.createObjectURL(blob) : null), 'image/jpeg', 1.0);
  });
};
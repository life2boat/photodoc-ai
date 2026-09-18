import React, { useState, useRef, useEffect } from 'react';
import { CheckCircle, RefreshCw } from 'lucide-react';
import { validateRussianPhone, PHONE_ERROR_MESSAGE } from '../utils/phoneValidation';
import { reachGoal } from '../lib/metrics';
import { redirectToPayment } from '../lib/robokassa';

export default function PolaroidWidget({ onSuccess, onReset }) {
  const [photos, setPhotos] = useState([]); // [{ id, file, url, zoom, rotation, x, y }]
  const [draggingId, setDraggingId] = useState(null);
  const [isConfirmed, setIsConfirmed] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [isSuccess, setIsSuccess] = useState(false);
  const [orderId, setOrderId] = useState(null);
  const [paymentAmount, setPaymentAmount] = useState(0);
  const [printFormat, setPrintFormat] = useState('10x15');
  const [isFileDragging, setIsFileDragging] = useState(false);
  const [error, setError] = useState(null);

  // Контакты клиента
  const [userName, setUserName] = useState('');
  const [userPhone, setUserPhone] = useState('');
  const [phoneError, setPhoneError] = useState('');
  const [userEmail, setUserEmail] = useState('');

  const widgetRef = useRef(null);
  const fileInputRef = useRef(null);

  const scrollToWidget = () => {
    setTimeout(() => {
      widgetRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, 100);
  };

  const API_BASE_URL = '/api';
  const PRICE_PER_PHOTO = 30;
  const PRINT_FORMATS = [
    { value: '9x13', label: '9x13 см' },
    { value: '10x15', label: '10x15 см' }
  ];

  const photoUrlsRef = useRef({});

  // Очистка памяти: отзываем ObjectURL при размонтировании компонента
  useEffect(() => {
    const urls = photoUrlsRef.current;
    return () => {
      Object.values(urls).forEach(url => URL.revokeObjectURL(url));
    };
  }, []);

  const handleFileChange = (e) => {
    addPhotos(e.target.files);
  };

  const addPhotos = (fileList) => {
    const selected = Array.from(fileList || []);
    if (selected.length + photos.length > 10) {
      setError("Максимум 10 фотографий");
      return;
    }
    setError(null);
    
    const newPhotos = selected.map(file => {
      const id = crypto.randomUUID();
      const url = URL.createObjectURL(file);
      photoUrlsRef.current[id] = url;
      return {
        id,
        file,
        url,
        zoom: 1.0,
        rotation: 0, // Новое поле для поворота
        x: 0,
        y: 0
      };
    });

    setPhotos(prev => [...prev, ...newPhotos]);
  };

  const handleFileDrop = (e) => {
    e.preventDefault();
    setIsFileDragging(false);
    addPhotos(e.dataTransfer.files);
  };

  const updatePhoto = (id, updates) => {
    setPhotos(prev => prev.map(p => p.id === id ? { ...p, ...updates } : p));
  };

  const removePhoto = (id) => {
    // Освобождаем ObjectURL и убираем из ref
    if (photoUrlsRef.current[id]) {
      URL.revokeObjectURL(photoUrlsRef.current[id]);
      delete photoUrlsRef.current[id];
    }
    setPhotos(prev => prev.filter(p => p.id !== id));
  };

  // Логика перемещения (Drag to Pan)
  const handlePointerDown = (id, e) => {
    e.target.setPointerCapture(e.pointerId);
    setDraggingId(id);
  };

  const handlePointerMove = (id, e) => {
    if (draggingId !== id) return;
    const photo = photos.find(p => p.id === id);
    if (!photo) return;
    updatePhoto(id, {
      x: photo.x + e.movementX / photo.zoom,
      y: photo.y + e.movementY / photo.zoom
    });
  };

  const handlePointerUp = () => setDraggingId(null);

  const handleSubmit = async (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (!isConfirmed || photos.length === 0) return;
    if (!userName.trim() || !userPhone.trim() || !userEmail.trim()) {
      setError("Пожалуйста, укажите ваше имя, телефон и email");
      return;
    }

    if (!validateRussianPhone(userPhone)) {
      setPhoneError(PHONE_ERROR_MESSAGE);
      return;
    }

    setIsLoading(true);
    setError(null);
    const formData = new FormData();
    formData.append('name', userName); 
    formData.append('phone', userPhone);
    formData.append('email', userEmail);
    formData.append('format', 'Polaroid');
    formData.append('print_format', printFormat);
    formData.append('paper', 'Глянцевая');
    formData.append('crop', 'С рамкой');
    
    const metaData = photos.map((p, index) => ({
      index,
      zoom: p.zoom,
      rotation: p.rotation,
      offsetX: p.x,
      offsetY: p.y
    }));
    
    formData.append('comment', `Формат бумаги: ${printFormat} см | Кадрирование: ${JSON.stringify(metaData)}`);
    photos.forEach(p => formData.append('files', p.file));

    try {
      const response = await fetch(`${API_BASE_URL}/order`, {
        method: 'POST',
        body: formData,
      });
      if (response.ok) {
        const data = await response.json();
        setOrderId(data.order_id || '...');
        setPaymentAmount(data.amount ? parseFloat(data.amount) : photos.length * PRICE_PER_PHOTO);
        setIsSuccess(true);
        scrollToWidget();
        reachGoal('ORDER_CREATED');
        if (onSuccess) onSuccess();
        photos.forEach(p => URL.revokeObjectURL(p.url));
        photoUrlsRef.current = {};
        setPhotos([]);
        setIsConfirmed(false);
        setUserName('');
        setUserPhone('');
        setPhoneError('');
        setUserEmail('');
        setPrintFormat('10x15');
        setError(null);
      } else {
        const errorData = await response.json();
        throw new Error(errorData.detail || "Ошибка сервера");
      }
    } catch (err) {
      setError("Ошибка: " + (err?.message || "Не удалось оформить заказ"));
    } finally {
      setIsLoading(false);
    }
  };

  if (isSuccess) {
    return (
      <div ref={widgetRef} className="bg-green-50 border border-green-200 text-green-800 rounded-xl p-8 text-center space-y-4 max-w-2xl mx-auto animate-fadeIn">
        <CheckCircle className="w-16 h-16 mx-auto text-green-500" />
        <h2 className="text-2xl font-bold animate-check-pop">Фото успешно отправлено в обработку</h2>
        <p className="text-green-700">Заказ №{orderId} сформирован. Перейдите к оплате для запуска в печать.</p>
        <div className="flex flex-col sm:flex-row gap-4 justify-center mt-6">
          <button
            type="button"
            onClick={() => redirectToPayment(orderId, paymentAmount)}
            className="px-8 py-3 bg-yellow-400 text-black font-bold rounded-full hover:bg-yellow-500 transition-colors shadow-sm"
          >
            Перейти к оплате
          </button>
          <button type="button" onClick={() => { setIsSuccess(false); setOrderId(null); setPaymentAmount(0); if (onReset) onReset(); }} className="px-6 py-3 bg-gray-200 text-gray-800 font-medium rounded-full hover:bg-gray-300 transition-colors">
            Оформить новый заказ
          </button>
        </div>
      </div>
    );
  }

  return (
    <form ref={widgetRef} onSubmit={handleSubmit} className="bg-gray-900 border border-gray-800 rounded-3xl p-8 shadow-2xl animate-fadeIn">
      <h2 className="text-2xl font-bold text-white mb-6">📸 Ретро Polaroid <span className="text-sm font-normal text-gray-500">(до 10 шт)</span></h2>

      {error && (
        <div className="mb-6 bg-red-950/80 border-l-4 border-red-500 text-red-200 p-3 rounded-md text-sm">
          {error}
        </div>
      )}

      {/* Кнопка загрузки */}
      <label
        role="button"
        tabIndex={0}
        aria-label="Добавить фотографии для ретро-печати"
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            fileInputRef.current?.click();
          }
        }}
        onDragOver={(e) => { e.preventDefault(); setIsFileDragging(true); }}
        onDragLeave={(e) => { e.preventDefault(); setIsFileDragging(false); }}
        onDrop={handleFileDrop}
        className={`group relative mb-8 flex min-h-40 w-full cursor-pointer flex-col items-center justify-center overflow-hidden rounded-3xl border border-dashed bg-[radial-gradient(circle_at_50%_0%,rgba(30,58,138,0.22),transparent_45%),linear-gradient(145deg,rgba(255,255,255,0.055),rgba(2,8,23,0.72))] p-8 text-center shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_0_40px_rgba(15,23,42,0.55)] backdrop-blur-xl transition-all duration-300 hover:-translate-y-1 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_0_48px_rgba(202,138,4,0.10)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-yellow-400 ${isFileDragging ? 'border-yellow-500/60' : 'border-white/15 hover:border-yellow-500/45'}`}
      >
        <span className={`pointer-events-none absolute inset-x-10 top-0 h-px bg-gradient-to-r from-transparent via-yellow-500/35 to-transparent transition-opacity ${isFileDragging ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'}`} />
        <span className="mb-4 flex h-14 w-14 items-center justify-center rounded-2xl border border-yellow-500/20 bg-yellow-500/10 text-3xl shadow-[0_0_28px_rgba(202,138,4,0.12)] transition-transform group-hover:scale-110">➕</span>
        <p className="text-sm font-semibold text-white">Добавить фотографии</p>
        <p className="mt-2 text-xs leading-5 text-zinc-400">До 10 снимков для ретро-печати</p>
        <p className="text-xs text-zinc-500">Поддерживаются JPG, PNG, WEBP</p>
        <input ref={fileInputRef} type="file" multiple accept="image/*" className="hidden" onChange={handleFileChange} />
      </label>

      {/* Сетка фото с рамками */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-8 mb-10">
        {isLoading && (
          <div className="col-span-full space-y-3 rounded-xl border border-white/10 bg-white/5 p-4">
            <div className="shimmer h-3 rounded-full bg-white/10" />
            <div className="shimmer h-3 w-2/3 rounded-full bg-white/10" />
          </div>
        )}
        {photos.map((photo) => (
          <div key={photo.id} className="flex flex-col gap-4">
            {/* СТАТИЧНАЯ РАМКА POLAROID */}
            <div className="bg-white p-3 pb-10 shadow-2xl mx-auto w-fit">
              <div 
                className="w-64 h-64 sm:w-72 sm:h-72 bg-gray-200 overflow-hidden relative cursor-move touch-none border border-gray-100"
                onPointerDown={(e) => handlePointerDown(photo.id, e)}
                onPointerMove={(e) => handlePointerMove(photo.id, e)}
                onPointerUp={handlePointerUp}
                onPointerCancel={handlePointerUp}
                onPointerLeave={handlePointerUp}
              >
                <img 
                  src={photo.url} 
                  alt="preview"
                  width="288"
                  height="288"
                  loading="lazy"
                  decoding="async"
                  draggable="false"
                  className="absolute max-w-none pointer-events-none origin-center"
                  style={{
                    // Порядок трансформации критически важен: сначала сдвиг, потом поворот, потом масштаб
                    transform: `translate(calc(-50% + ${photo.x}px), calc(-50% + ${photo.y}px)) rotate(${photo.rotation}deg) scale(${photo.zoom})`,
                    top: '50%',
                    left: '50%',
                    width: '100%'
                  }}
                />
                <div className="absolute inset-0 border-4 border-transparent hover:border-yellow-400/30 transition-colors pointer-events-none" />
              </div>
              <p className="text-center mt-4 font-mono text-gray-400 text-xs font-bold uppercase">
                Photo {photos.indexOf(photo) + 1}
              </p>
            </div>

            {/* Блок управления фото (Зум и Поворот) */}
            <div className="bg-gray-950 p-5 rounded-2xl border border-gray-800 space-y-4 shadow-inner max-w-sm mx-auto w-full">
              
              {/* Зум */}
              <div>
                <div className="flex justify-between text-xs text-gray-400 mb-2 font-bold">
                  <span>Масштаб</span>
                  <span className="text-yellow-400">{Math.round(photo.zoom * 100)}%</span>
                </div>
                <input 
                  type="range" min="0.5" max="3" step="0.01" 
                  value={photo.zoom}
                  onChange={(e) => updatePhoto(photo.id, { zoom: parseFloat(e.target.value) })}
                  className="w-full h-2 bg-gray-800 rounded-lg appearance-none cursor-pointer accent-yellow-400"
                />
              </div>

              {/* Поворот */}
              <div>
                <div className="flex justify-between text-xs text-gray-400 mb-2 font-bold">
                  <span>Поворот</span>
                  <span className="text-yellow-400">{photo.rotation}°</span>
                </div>
                <input 
                  type="range" min="-180" max="180" step="1" 
                  value={photo.rotation}
                  onChange={(e) => updatePhoto(photo.id, { rotation: parseInt(e.target.value) })}
                  className="w-full h-2 bg-gray-800 rounded-lg appearance-none cursor-pointer accent-yellow-400"
                />
              </div>

              {/* Удалить */}
              <button 
                type="button"
                onClick={() => removePhoto(photo.id)}
                className="w-full py-2 text-sm font-bold text-red-500 bg-red-500/10 hover:bg-red-500/20 rounded-xl transition mt-2"
              >
                Удалить снимок
              </button>
            </div>
          </div>
        ))}
      </div>

      {/* Блок подтверждения заказа (Итого) */}
      {photos.length > 0 && (
        <div className="mt-8 p-6 bg-gray-950 border border-gray-800 rounded-2xl shadow-xl">
          <div className="mb-5">
            <p className="text-sm font-medium text-gray-400 mb-3">Формат бумаги</p>
            <div className="grid grid-cols-2 gap-3">
              {PRINT_FORMATS.map((formatOption) => {
                const isSelected = printFormat === formatOption.value;

                return (
                  <label
                    key={formatOption.value}
                    className={`flex items-center justify-center gap-2 rounded-xl border px-4 py-3 text-sm font-bold transition-colors cursor-pointer ${
                      isSelected
                        ? 'border-yellow-400 bg-yellow-400 text-black'
                        : 'border-gray-800 bg-gray-900 text-gray-300 hover:border-yellow-400/70'
                    }`}
                  >
                    <input
                      type="radio"
                      name="polaroid-print-format"
                      value={formatOption.value}
                      checked={isSelected}
                      onChange={(e) => setPrintFormat(e.target.value)}
                      disabled={isLoading}
                      className="sr-only"
                    />
                    {formatOption.label}
                  </label>
                );
              })}
            </div>
          </div>

          <div className="mb-6 rounded-xl border border-yellow-400/20 bg-yellow-400/10 px-4 py-3 text-sm leading-relaxed text-yellow-100">
            ℹ️ Обратите внимание: снимки печатаются в стиле ретро, поэтому на бумаге выбранного формата останутся характерные белые поля (рамка).
          </div>

          <div className="flex justify-between items-center mb-6 border-b border-gray-800 pb-4">
            <h3 className="text-xl font-bold text-white">Итого к оплате:</h3>
            <div className="text-right">
              <span className="text-2xl font-bold text-yellow-400">{photos.length * PRICE_PER_PHOTO} ₽</span>
              <p className="text-gray-500 text-sm mt-1">{photos.length} шт. x {PRICE_PER_PHOTO} ₽</p>
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-6">
            <div>
              <label htmlFor="polaroid-name" className="block text-sm font-medium text-gray-400 mb-1">Ваше имя *</label>
              <input id="polaroid-name" type="text" value={userName} onChange={(e) => setUserName(e.target.value)} placeholder="Иван" disabled={isLoading} className="w-full bg-gray-900 border border-gray-800 text-white rounded-xl px-4 py-3 focus:outline-none focus:border-yellow-400 transition-colors" />
            </div>
            <div>
              <label htmlFor="polaroid-phone" className="block text-sm font-medium text-gray-400 mb-1">Телефон *</label>
              <input
                id="polaroid-phone"
                type="tel"
                value={userPhone}
                onChange={(e) => {
                  setUserPhone(e.target.value);
                  setPhoneError('');
                }}
                placeholder="+7 (999) 000-00-00"
                disabled={isLoading}
                className={`w-full rounded-xl border bg-gray-900 px-4 py-3 text-white transition-colors focus:outline-none ${
                  phoneError
                    ? 'border-red-500/50 focus:border-red-500 focus:ring-2 focus:ring-red-500/50'
                    : 'border-gray-800 focus:border-yellow-400'
                }`}
              />
              <p className={`mt-1 text-xs text-red-400 transition-all duration-200 ${phoneError ? 'opacity-100' : 'opacity-0'}`}>
                {phoneError || '\u00A0'}
              </p>
            </div>
            <div>
              <label htmlFor="polaroid-email" className="block text-sm font-medium text-gray-400 mb-1">Email для подтверждения *</label>
              <input id="polaroid-email" type="email" value={userEmail} onChange={(e) => setUserEmail(e.target.value)} placeholder="ivan@example.ru" disabled={isLoading} className="w-full bg-gray-900 border border-gray-800 text-white rounded-xl px-4 py-3 focus:outline-none focus:border-yellow-400 transition-colors" />
            </div>
          </div>

          <div className="flex items-center gap-4 mb-6 p-4 bg-gray-900 rounded-xl border border-gray-800">
            <input
              type="checkbox" id="confirm"
              checked={isConfirmed}
              onChange={(e) => setIsConfirmed(e.target.checked)}
              className="w-6 h-6 accent-yellow-400 rounded cursor-pointer shrink-0"
            />
            <label htmlFor="confirm" className="text-gray-300 text-sm cursor-pointer select-none">
              Я откадрировал(а) все {photos.length} фото и подтверждаю их правильность.
            </label>
          </div>

          <button
            type="submit"
            disabled={!isConfirmed || isLoading || !userName.trim() || !userPhone.trim() || !userEmail.trim()}
            className={`w-full py-4 rounded-xl font-bold text-lg transition-all duration-300 flex justify-center items-center gap-2 ${
              isConfirmed && !isLoading
                ? 'bg-yellow-400 text-black hover:bg-yellow-500 shadow-lg hover:shadow-yellow-400/20 hover:scale-[1.02]'
                : 'bg-gray-800 text-gray-600 cursor-not-allowed'
            }`}
          >
            {isLoading ? (
              <span className="animate-pulse">Отправка файлов...</span>
            ) : (
              'Отправить заказ в обработку'
            )}
          </button>
        </div>
      )}
    </form>
  );
}

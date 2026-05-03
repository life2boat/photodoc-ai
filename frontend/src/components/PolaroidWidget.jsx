import React, { useState, useEffect } from 'react';
import { CheckCircle } from 'lucide-react';

export default function PolaroidWidget({ onSuccess, onReset }) {
  const [photos, setPhotos] = useState([]);
  const [isConfirmed, setIsConfirmed] = useState(false);
  const [isLoading, setIsLoading] = useState(false);
  const [draggingId, setDraggingId] = useState(null);
  const [userName, setUserName] = useState('');
  const [userPhone, setUserPhone] = useState('');
  const [isSuccess, setIsSuccess] = useState(false);
  const [orderId, setOrderId] = useState(null);
  
  const PRICE_PER_PHOTO = 50;

  // Очистка памяти при размонтировании согласно gemini.md
  useEffect(() => {
    return () => {
      photos.forEach(photo => URL.revokeObjectURL(photo.url));
    };
  }, [photos]);

  const handleFileChange = (e) => {
    const selected = Array.from(e.target.files);
    if (selected.length + photos.length > 10) {
      alert("Максимум 10 фотографий");
      return;
    }
    
    const newPhotos = selected.map(file => ({
      id: Math.random().toString(36).substr(2, 9),
      file,
      url: URL.createObjectURL(file),
      zoom: 1.0, 
      rotation: 0, // Новое поле для поворота
      x: 0,
      y: 0
    }));

    setPhotos(prev => [...prev, ...newPhotos]);
  };

  const updatePhoto = (id, updates) => {
    setPhotos(prev => prev.map(p => p.id === id ? { ...p, ...updates } : p));
  };

  const removePhoto = (id) => {
    // ВАЖНО: Освобождаем память перед удалением снимка
    const photoToRemove = photos.find(p => p.id === id);
    if (photoToRemove) URL.revokeObjectURL(photoToRemove.url);
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

    // Смещение с учетом текущего масштаба
    updatePhoto(id, {
      x: photo.x + e.movementX / photo.zoom,
      y: photo.y + e.movementY / photo.zoom
    });
  };

  const handlePointerUp = () => setDraggingId(null);

  const handleSubmit = async (e) => {
    e.preventDefault();
    e.stopPropagation(); // Отключаем всплытие эвента к родительским формам
    if (!isConfirmed || photos.length === 0) return;
    if (!userName.trim() || !userPhone.trim()) {
      alert("Пожалуйста, укажите ваше имя и телефон");
      return;
    }

    setIsLoading(true);
    const formData = new FormData();
    formData.append('name', userName); 
    formData.append('phone', userPhone);
    formData.append('format', 'Polaroid');
    formData.append('paper', 'Глянцевая');
    formData.append('crop', 'С рамкой');
    
    const metaData = photos.map((p, index) => ({
      index,
      zoom: p.zoom,
      rotation: p.rotation,
      offsetX: p.x,
      offsetY: p.y
    }));
    
    formData.append('comment', `Кадрирование: ${JSON.stringify(metaData)}`);
    photos.forEach(p => formData.append('files', p.file));

    try {
      const response = await fetch('http://localhost:8000/api/order', {
        method: 'POST',
        body: formData,
      });
      if (response.ok) {
        const data = await response.json();
        setOrderId(data.order_id || '...');
        setIsSuccess(true);
        if (onSuccess) onSuccess();
        photos.forEach(p => URL.revokeObjectURL(p.url));
        setPhotos([]);
        setIsConfirmed(false);
        setUserName('');
        setUserPhone('');
      } else {
        const errorData = await response.json();
        throw new Error(errorData.detail || "Ошибка сервера");
      }
    } catch (err) {
      alert("Ошибка: " + err.message);
    } finally {
      setIsLoading(false);
    }
  };

  if (isSuccess) {
    return (
      <div className="bg-green-50 border border-green-200 text-green-800 rounded-xl p-8 text-center space-y-4 max-w-2xl mx-auto animate-fadeIn">
        <CheckCircle className="w-16 h-16 mx-auto text-green-500" />
        <h2 className="text-2xl font-bold">Ваш заказ №{orderId} сформирован!</h2>
        <p className="text-green-700">Перейдите к оплате для запуска в печать.</p>
        <div className="flex flex-col sm:flex-row gap-4 justify-center mt-6">
          <button type="button" onClick={() => alert('Здесь будет редирект на ЮKassa/Robokassa')} className="px-8 py-3 bg-yellow-400 text-black font-bold rounded-full hover:bg-yellow-500 transition-colors shadow-sm">
            Перейти к оплате
          </button>
          <button type="button" onClick={() => { setIsSuccess(false); setOrderId(null); if (onReset) onReset(); }} className="px-6 py-3 bg-gray-200 text-gray-800 font-medium rounded-full hover:bg-gray-300 transition-colors">
            Оформить новый заказ
          </button>
        </div>
      </div>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="bg-gray-900 border border-gray-800 rounded-3xl p-8 shadow-2xl animate-fadeIn">
      <h2 className="text-2xl font-bold text-white mb-6">📸 Ретро Polaroid <span className="text-sm font-normal text-gray-500">(до 10 шт)</span></h2>

      {/* Кнопка загрузки */}
      <label className="flex flex-col items-center justify-center w-full h-32 border-2 border-dashed border-gray-700 rounded-2xl cursor-pointer hover:border-yellow-400 hover:bg-gray-850 transition-all mb-8 group">
        <span className="text-3xl mb-2 group-hover:scale-110 transition">➕</span>
        <p className="text-sm text-gray-400">Добавить фотографии</p>
        <input type="file" multiple accept="image/*" className="hidden" onChange={handleFileChange} />
      </label>

      {/* Сетка фото с рамками */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-8 mb-10">
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
          <div className="flex justify-between items-center mb-6 border-b border-gray-800 pb-4">
            <h3 className="text-xl font-bold text-white">Итого к оплате:</h3>
            <div className="text-right">
              <span className="text-2xl font-bold text-yellow-400">{photos.length * PRICE_PER_PHOTO} ₽</span>
              <p className="text-gray-500 text-sm mt-1">{photos.length} шт. x {PRICE_PER_PHOTO} ₽</p>
            </div>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-6">
            <div>
              <label className="block text-sm font-medium text-gray-400 mb-1">Ваше имя *</label>
              <input type="text" value={userName} onChange={(e) => setUserName(e.target.value)} placeholder="Иван" disabled={isLoading} className="w-full bg-gray-900 border border-gray-800 text-white rounded-xl px-4 py-3 focus:outline-none focus:border-yellow-400 transition-colors" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-400 mb-1">Телефон *</label>
              <input type="tel" value={userPhone} onChange={(e) => setUserPhone(e.target.value)} placeholder="+7 (999) 000-00-00" disabled={isLoading} className="w-full bg-gray-900 border border-gray-800 text-white rounded-xl px-4 py-3 focus:outline-none focus:border-yellow-400 transition-colors" />
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
            disabled={!isConfirmed || isLoading || !userName.trim() || !userPhone.trim()}
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
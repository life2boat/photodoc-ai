import React, { useState, useRef, useEffect } from 'react';
import { UploadCloud, Loader2, Trash2, X } from 'lucide-react';
import { redirectToPayment } from '../lib/robokassa';
import { API_BASE_URL } from '../config';
import { reachGoal } from '../lib/metrics';
import { PHONE_ERROR_MESSAGE, validateRussianPhone } from '../utils/phoneValidation';

const DOC_PRICES = {
  '3x4': 300,
  '3.5x4.5': 300,
  '4x6': 300,
  '9x12': 300
};

const DOC_FORMATS = [
  { id: '3x4', label: '3х4 см (стандартный)' },
  { id: '3.5x4.5', label: '3.5х4.5 см (паспорт/виза)' },
  { id: '4x6', label: '4х6 см' },
  { id: '9x12', label: '9х12 см (личное дело)' }
];

const FilePreview = ({ file, onRemove }) => {
  const [previewUrl, setPreviewUrl] = useState('');

  useEffect(() => {
    const objectUrl = URL.createObjectURL(file);
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setPreviewUrl(objectUrl);

    return () => {
      URL.revokeObjectURL(objectUrl);
    };
  }, [file]);

  return (
    <div className="relative group aspect-square">
      {previewUrl && (
        <img
          src={previewUrl}
          alt="preview"
          width="320"
          height="320"
          loading="lazy"
          decoding="async"
          className="w-full h-full object-cover rounded-lg border border-neutral-200 shadow-sm"
        />
      )}
      <button
        type="button"
        onClick={onRemove}
        aria-label="Удалить файл"
        className="absolute -top-2 -right-2 bg-red-500 text-white rounded-full p-1 opacity-0 group-hover:opacity-100 transition-opacity shadow-md hover:bg-red-600"
        title="Удалить фото"
      >
        <X size={14} strokeWidth={3} />
      </button>
    </div>
  );
};

export function PhotoDocWidget({ onSuccess, onReset }) {
  const [files, setFiles] = useState([]);
  const [isLoading, setIsLoading] = useState(false);
  const [isSuccess, setIsSuccess] = useState(false);
  const [error, setError] = useState(null);
  const [orderId, setOrderId] = useState(null);
  const [paymentAmount, setPaymentAmount] = useState(0);
  const [isDragging, setIsDragging] = useState(false);

  const [userName, setUserName] = useState('');
  const [userPhone, setUserPhone] = useState('');
  const [phoneError, setPhoneError] = useState('');
  const [userEmail, setUserEmail] = useState('');
  const [userComment, setUserComment] = useState('');
  const [docType, setDocType] = useState('3.5x4.5');

  const fileInputRef = useRef(null);
  const widgetRef = useRef(null);

  const scrollToWidget = () => {
    setTimeout(() => {
      widgetRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, 100);
  };

  const addFiles = (fileList) => {
    const newFiles = Array.from(fileList).filter(f => f.type.startsWith("image/"));
    if (newFiles.length === 0) return;
    setFiles(prev => [...prev, ...newFiles]);
    setError(null);
    setIsSuccess(false);
  };

  const handleDrop = (e) => {
    e.preventDefault();
    setIsDragging(false);
    addFiles(e.dataTransfer.files);
  };

  const handleSubmitOrder = async () => {
    if (files.length === 0 || !userName.trim() || !userPhone.trim() || !userEmail.trim()) return;

    if (!validateRussianPhone(userPhone)) {
      setPhoneError(PHONE_ERROR_MESSAGE);
      return;
    }

    setIsLoading(true);
    setError(null);
    
    const selectedFormatLabel = DOC_FORMATS.find(f => f.id === docType)?.label || docType;
    const price = DOC_PRICES[docType] || 0;

    const formData = new FormData();
    formData.append('name', userName);
    formData.append('phone', userPhone);
    formData.append('email', userEmail);
    formData.append('format', selectedFormatLabel);
    formData.append('paper', 'Цифровая обработка');
    formData.append('crop', 'По стандарту документа');
    
    const finalComment = `🪪 ФОТО НА ДОКУМЕНТЫ | Тип: ${selectedFormatLabel} | Сумма: ${price} руб.` + (userComment ? ` | Комментарий клиента: ${userComment}` : '');
    formData.append('comment', finalComment);

    files.forEach(f => {
      formData.append("files", f);
    });

    try {
      const apiResponse = await fetch(`${API_BASE_URL}/order`, {
        method: 'POST',
        body: formData,
      });

      if (!apiResponse.ok) {
        const err = await apiResponse.json().catch(() => null);
        throw new Error(err?.detail || 'Ошибка при отправке заказа');
      }

      const data = await apiResponse.json();
      setOrderId(data.order_id || '...');
      setPaymentAmount(price);
      setIsSuccess(true);
      scrollToWidget();
      reachGoal('ORDER_CREATED');
      if (onSuccess) onSuccess();
    } catch (err) {
      console.error(err);
      setError(err.message || 'Ошибка соединения с сервером');
    } finally {
      setIsLoading(false);
    }
  };

  const handleReset = () => {
    setFiles([]);
    setIsSuccess(false);
    setUserName('');
    setUserPhone('');
    setPhoneError('');
    setUserEmail('');
    setUserComment('');
    setError(null);
    setOrderId(null);
    setPaymentAmount(0);
    if (onReset) onReset();
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  };

  if (isSuccess) {
    return (
      <div ref={widgetRef} className="bg-green-50 border border-green-200 text-green-800 rounded-xl p-8 text-center space-y-4 animate-in fade-in zoom-in duration-300 max-w-2xl mx-auto">
        <h2 className="text-2xl font-bold animate-check-pop">✅ Фото успешно отправлено в обработку</h2>
        <p className="text-green-700">Заказ №{orderId} принят. Мы подготовим фото по стандартам и свяжемся с вами.</p>
        <div className="flex flex-col sm:flex-row gap-4 justify-center mt-6">
          <button
            type="button"
            onClick={() => redirectToPayment(orderId, paymentAmount)}
            className="px-8 py-3 bg-yellow-400 text-black font-bold rounded-full hover:bg-yellow-500 transition-colors shadow-sm"
          >
            Перейти к оплате
          </button>
          <button onClick={handleReset} className="px-6 py-3 bg-green-600 text-white font-medium rounded-full hover:bg-green-700 transition-colors">
            Оформить еще один заказ
          </button>
        </div>
      </div>
    );
  }

  return (
    <div ref={widgetRef} className="w-full space-y-6 max-w-4xl mx-auto">
      {files.length === 0 ? (
        <div
          onDragOver={(e) => { e.preventDefault(); setIsDragging(true); }}
          onDragLeave={(e) => { e.preventDefault(); setIsDragging(false); }}
          onDrop={handleDrop}
          className={`group relative mx-auto max-w-3xl cursor-pointer overflow-hidden rounded-3xl border border-dashed bg-[radial-gradient(circle_at_50%_0%,rgba(30,58,138,0.22),transparent_45%),linear-gradient(145deg,rgba(255,255,255,0.055),rgba(2,8,23,0.72))] p-10 text-center shadow-[inset_0_1px_0_rgba(255,255,255,0.06),0_0_40px_rgba(15,23,42,0.55)] backdrop-blur-xl transition-all duration-300 hover:-translate-y-1 hover:shadow-[inset_0_1px_0_rgba(255,255,255,0.08),0_0_48px_rgba(202,138,4,0.10)] ${isDragging ? 'border-yellow-500/60 bg-yellow-500/10' : 'border-white/15 hover:border-yellow-500/45'}`}
          onClick={() => fileInputRef.current?.click()}
        >
          <div className={`pointer-events-none absolute inset-x-10 top-0 h-px bg-gradient-to-r from-transparent via-yellow-500/35 to-transparent transition-opacity ${isDragging ? 'opacity-100' : 'opacity-0 group-hover:opacity-100'}`} />
          <input type="file" ref={fileInputRef} onChange={(e) => addFiles(e.target.files)} accept="image/png, image/jpeg, image/webp" multiple className="hidden" />
          <div className="relative flex flex-col items-center justify-center text-zinc-300">
            <div className="mb-5 flex h-16 w-16 items-center justify-center rounded-2xl border border-yellow-500/20 bg-yellow-500/10 shadow-[0_0_28px_rgba(202,138,4,0.12)] transition-transform group-hover:scale-110">
              <UploadCloud className="w-8 h-8 text-yellow-300" />
            </div>
            <p className="font-semibold text-white">Нажмите для выбора фото</p>
            <p className="mt-2 text-sm leading-6 text-zinc-400">Можно загрузить сразу несколько фотографий</p>
            <p className="text-xs text-zinc-500">Поддерживаются JPG, PNG, WEBP</p>
          </div>
        </div>
      ) : (
        <div className="space-y-6 animate-in fade-in duration-300 grid grid-cols-1 md:grid-cols-2 gap-8 items-start">
          <div className="bg-gray-900 rounded-xl p-4 flex flex-col">
            <div className="flex justify-between items-center mb-3">
              <h3 className="font-semibold text-gray-300">Выбрано фото: {files.length} шт.</h3>
              <div className="flex gap-2">
                <button onClick={() => fileInputRef.current?.click()} disabled={isLoading} className="text-yellow-400 hover:text-yellow-300 text-sm font-medium transition-colors">
                  + Добавить
                </button>
                <button onClick={handleReset} disabled={isLoading} className="text-gray-500 hover:text-red-500 p-1 transition-colors" title="Удалить все" aria-label="Удалить все фото">
                  <Trash2 className="w-5 h-5" />
                </button>
              </div>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 overflow-y-auto max-h-96 p-2">
              {isLoading && (
                <div className="col-span-full space-y-3 rounded-xl border border-white/10 bg-white/5 p-4">
                  <div className="shimmer h-3 rounded-full bg-white/10" />
                  <div className="shimmer h-3 w-2/3 rounded-full bg-white/10" />
                </div>
              )}
              {files.map((f, index) => (
                <FilePreview
                  key={index}
                  file={f}
                  onRemove={() => {
                    const newFiles = [...files];
                    newFiles.splice(index, 1);
                    setFiles(newFiles);
                  }}
                />
              ))}
            </div>
          </div>

          <div className="flex flex-col gap-5">
            <h3 className="text-lg font-bold text-white border-b border-gray-800 pb-2">Оформление заказа</h3>
            
            {error && ( <div className="bg-red-100 border-l-4 border-red-500 text-red-700 p-3 rounded-md text-sm">{error}</div> )}

            <div>
              <label className="block text-sm font-medium text-gray-400 mb-1">Тип документа</label>
              <select value={docType} onChange={(e) => setDocType(e.target.value)} disabled={isLoading} className="w-full border border-gray-700 rounded-lg px-4 py-2.5 focus:outline-none focus:ring-2 focus:ring-yellow-400 bg-gray-800 text-white">
                {DOC_FORMATS.map(format => (<option key={format.id} value={format.id}>{format.label} — {DOC_PRICES[format.id]} ₽</option>))}
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-400 mb-1">Ваше имя</label>
              <input type="text" value={userName} onChange={(e) => setUserName(e.target.value)} placeholder="Иван Иванов" disabled={isLoading} className="w-full border border-gray-700 rounded-lg px-4 py-2.5 focus:outline-none focus:ring-2 focus:ring-yellow-400 bg-gray-800 text-white disabled:opacity-50" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-400 mb-1">Телефон</label>
              <input
                type="tel"
                value={userPhone}
                onChange={(e) => {
                  setUserPhone(e.target.value);
                  setPhoneError('');
                }}
                placeholder="+7 (999) 000-00-00"
                disabled={isLoading}
                className={`w-full rounded-lg border bg-gray-800 px-4 py-2.5 text-white transition-colors focus:outline-none focus:ring-2 disabled:opacity-50 ${
                  phoneError
                    ? 'border-red-500/50 focus:border-red-500 focus:ring-red-500/50'
                    : 'border-gray-700 focus:ring-yellow-400'
                }`}
              />
              <p className={`mt-1 text-xs text-red-400 transition-all duration-200 ${phoneError ? 'opacity-100' : 'opacity-0'}`}>
                {phoneError || '\u00A0'}
              </p>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-400 mb-1">Email для подтверждения</label>
              <input type="email" value={userEmail} onChange={(e) => setUserEmail(e.target.value)} placeholder="ivan@example.ru" disabled={isLoading} className="w-full border border-gray-700 rounded-lg px-4 py-2.5 focus:outline-none focus:ring-2 focus:ring-yellow-400 bg-gray-800 text-white disabled:opacity-50" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-400 mb-1">Комментарий к заказу</label>
              <textarea value={userComment} onChange={(e) => setUserComment(e.target.value)} placeholder="Например: сделать в костюме, убрать прыщик" disabled={isLoading} className="w-full border border-gray-700 rounded-lg px-4 py-2.5 focus:outline-none focus:ring-2 focus:ring-yellow-400 bg-gray-800 text-white disabled:opacity-50 resize-none h-24" />
            </div>
            <button onClick={handleSubmitOrder} disabled={isLoading || files.length === 0 || !userName.trim() || !userPhone.trim() || !userEmail.trim()} className="w-full mt-2 bg-yellow-400 text-black font-bold py-3.5 px-4 rounded-lg flex items-center justify-center hover:bg-yellow-300 transition-all disabled:bg-gray-500 disabled:text-gray-300 disabled:cursor-not-allowed">
              {isLoading ? ( <><Loader2 className="mr-2 h-5 w-5 animate-spin" /> Отправка...</> ) : ( 'Оформить заказ' )}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

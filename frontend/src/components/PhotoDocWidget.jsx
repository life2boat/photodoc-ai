import React, { useState, useRef, useEffect } from 'react';
import { UploadCloud, Loader2, Trash2, X } from 'lucide-react';

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
  const [previewUrl, setPreviewUrl] = useState(null);

  useEffect(() => {
    const url = URL.createObjectURL(file);
    setPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  return (
    <div className="relative group aspect-square">
      {previewUrl && (
        <img
          src={previewUrl} // eslint-disable-line
          alt="preview"
          className="w-full h-full object-cover rounded-lg border border-neutral-200 shadow-sm"
        />
      )}
      <button
        type="button"
        onClick={onRemove}
        className="absolute -top-2 -right-2 bg-red-500 text-white rounded-full p-1 opacity-0 group-hover:opacity-100 transition-opacity shadow-md hover:bg-red-600"
        title="Удалить фото"
      >
        <X size={14} strokeWidth={3} />
      </button>
    </div>
  );
};

export function PhotoDocWidget() {
  const [files, setFiles] = useState([]);
  const [isLoading, setIsLoading] = useState(false);
  const [isSuccess, setIsSuccess] = useState(false);
  const [error, setError] = useState(null);
  const [orderId, setOrderId] = useState(null);

  const [userName, setUserName] = useState('');
  const [userPhone, setUserPhone] = useState('');
  const [userComment, setUserComment] = useState('');
  const [docType, setDocType] = useState('3.5x4.5');

  const fileInputRef = useRef(null);

  const handleFileSelect = (e) => {
    if (e.target.files && e.target.files.length > 0) {
      const newFiles = Array.from(e.target.files).filter(f => f.type.startsWith("image/"));
      setFiles(prev => [...prev, ...newFiles]);
      setError(null);
      setIsSuccess(false);
    }
  };

  const handleSubmitOrder = async () => {
    if (files.length === 0 || !userName.trim() || !userPhone.trim()) return;

    setIsLoading(true);
    setError(null);
    
    const selectedFormatLabel = DOC_FORMATS.find(f => f.id === docType)?.label || docType;
    const price = DOC_PRICES[docType] || 0;

    const formData = new FormData();
    formData.append('name', userName);
    formData.append('phone', userPhone);
    
    const finalComment = `🪪 ФОТО НА ДОКУМЕНТЫ | Тип: ${selectedFormatLabel} | Сумма: ${price} руб.` + (userComment ? ` | Комментарий клиента: ${userComment}` : '');
    formData.append('comment', finalComment);

    files.forEach(f => {
      formData.append("files", f);
    });

    try {
      const apiResponse = await fetch('http://localhost:8000/api/orders', {
        method: 'POST',
        body: formData,
      });

      if (!apiResponse.ok) {
        const err = await apiResponse.json().catch(() => null);
        throw new Error(err?.detail || 'Ошибка при отправке заказа');
      }

      const data = await apiResponse.json();
      setOrderId(data.order_id || '...');
      setIsSuccess(true);
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
    setUserComment('');
    setError(null);
    setOrderId(null);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  };

  if (isSuccess) {
    return (
      <div className="bg-green-50 border border-green-200 text-green-800 rounded-xl p-8 text-center space-y-4 animate-in fade-in zoom-in duration-300 max-w-2xl mx-auto">
        <h2 className="text-2xl font-bold">✅ Заказ принят!</h2>
        <p className="text-green-700">Мы подготовим фото по стандартам и свяжемся с вами в течение 15 минут.</p>
        <button onClick={handleReset} className="mt-4 px-6 py-3 bg-green-600 text-white font-medium rounded-full hover:bg-green-700 transition-colors">
          Оформить еще один заказ
        </button>
      </div>
    );
  }

  return (
    <div className="w-full space-y-6 max-w-4xl mx-auto">
      {files.length === 0 ? (
        <div
          className="bg-gray-900 rounded-2xl shadow-sm border-2 border-dashed border-gray-700 p-8 text-center cursor-pointer hover:border-yellow-400 transition-colors max-w-2xl mx-auto"
          onClick={() => fileInputRef.current?.click()}
        >
          <input type="file" ref={fileInputRef} onChange={handleFileSelect} accept="image/png, image/jpeg, image/webp" multiple className="hidden" />
          <div className="flex flex-col items-center justify-center text-gray-400">
            <UploadCloud className="w-12 h-12 mb-4 text-neutral-400" />
            <p className="font-semibold text-neutral-700">Нажмите для выбора фото</p>
            <p className="text-sm">Можно загрузить сразу несколько фотографий</p>
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
                <button onClick={handleReset} disabled={isLoading} className="text-gray-500 hover:text-red-500 p-1 transition-colors" title="Удалить все">
                  <Trash2 className="w-5 h-5" />
                </button>
              </div>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3 overflow-y-auto max-h-96 p-2">
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
              <input type="tel" value={userPhone} onChange={(e) => setUserPhone(e.target.value)} placeholder="+7 (999) 000-00-00" disabled={isLoading} className="w-full border border-gray-700 rounded-lg px-4 py-2.5 focus:outline-none focus:ring-2 focus:ring-yellow-400 bg-gray-800 text-white disabled:opacity-50" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-400 mb-1">Комментарий к заказу</label>
              <textarea value={userComment} onChange={(e) => setUserComment(e.target.value)} placeholder="Например: сделать в костюме, убрать прыщик" disabled={isLoading} className="w-full border border-gray-700 rounded-lg px-4 py-2.5 focus:outline-none focus:ring-2 focus:ring-yellow-400 bg-gray-800 text-white disabled:opacity-50 resize-none h-24" />
            </div>
            <button onClick={handleSubmitOrder} disabled={isLoading || files.length === 0 || !userName.trim() || !userPhone.trim()} className="w-full mt-2 bg-yellow-400 text-black font-bold py-3.5 px-4 rounded-lg flex items-center justify-center hover:bg-yellow-300 transition-all disabled:bg-gray-500 disabled:text-gray-300 disabled:cursor-not-allowed">
              {isLoading ? ( <><Loader2 className="mr-2 h-5 w-5 animate-spin" /> Отправка...</> ) : ( 'Оформить заказ' )}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
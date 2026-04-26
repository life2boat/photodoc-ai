import React, { useState, useRef, useEffect } from "react";
import { UploadCloud, Loader2, Image as ImageIcon, Trash2, X } from "lucide-react";

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

export function RestoreWidget() {
  const [files, setFiles] = useState([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState(null);
  const fileInputRef = useRef(null);

  const [userName, setUserName] = useState("");
  const [userPhone, setUserPhone] = useState("");
  const [userComment, setUserComment] = useState("");
  const [isSuccess, setIsSuccess] = useState(false);

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
    
    const formData = new FormData();
    formData.append("name", userName);
    formData.append("phone", userPhone);
    
    const finalComment = `🛠 РЕСТАВРАЦИЯ` + (userComment ? ` | Комментарий клиента: ${userComment}` : '');
    formData.append("comment", finalComment);

    files.forEach(f => {
      formData.append("files", f);
    });

    try {
      const response = await fetch("http://localhost:8000/api/orders", {
        method: "POST",
        body: formData,
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => null);
        throw new Error(errorData?.detail || "Произошла ошибка при отправке заказа");
      }

      setIsSuccess(true);
    } catch (err) {
      setError(err.message);
      console.error("Ошибка при оформлении заказа:", err);
    } finally {
      setIsLoading(false);
    }
  };

  const handleReset = () => {
    setFiles([]);
    setError(null);
    setUserName("");
    setUserPhone("");
    setUserComment("");
    setIsSuccess(false);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  };

  if (isSuccess) {
    return (
      <div className="bg-green-50 border border-green-200 text-green-800 rounded-xl p-8 text-center space-y-4 animate-in fade-in zoom-in duration-300 max-w-2xl mx-auto">
        <h2 className="text-2xl font-bold">✅ Заказ принят!</h2>
        <p className="text-green-700">Мы изучим ваше фото и свяжемся с вами для уточнения стоимости и сроков.</p>
        <button onClick={handleReset} className="mt-4 px-6 py-3 bg-green-600 text-white font-medium rounded-full hover:bg-green-700 transition-colors">
          Отправить еще одно фото
        </button>
      </div>
    );
  }

  return (
    <div className="w-full space-y-6">
      {files.length === 0 ? (
        <div
          className="bg-gray-900 rounded-2xl shadow-sm border-2 border-dashed border-gray-700 p-8 text-center cursor-pointer hover:border-yellow-400 transition-colors max-w-2xl mx-auto"
          onClick={() => fileInputRef.current?.click()}
        >
          <input
            type="file"
            ref={fileInputRef}
            onChange={handleFileSelect}
            accept="image/png, image/jpeg, image/webp"
            multiple
            className="hidden"
          />
          <div className="flex flex-col items-center justify-center text-gray-400">
            <UploadCloud className="w-12 h-12 mb-4 text-neutral-400" />
            <p className="font-semibold text-neutral-700">Нажмите для выбора файла</p>
            <p className="text-sm">Поддерживаются JPG, PNG, WEBP (до 15 МБ)</p>
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
              <label className="block text-sm font-medium text-gray-400 mb-1">Ваше имя</label>
              <input type="text" value={userName} onChange={(e) => setUserName(e.target.value)} placeholder="Иван Иванов" disabled={isLoading} className="w-full border border-gray-700 rounded-lg px-4 py-2.5 focus:outline-none focus:ring-2 focus:ring-yellow-400 bg-gray-800 text-white disabled:opacity-50" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-400 mb-1">Телефон</label>
              <input type="tel" value={userPhone} onChange={(e) => setUserPhone(e.target.value)} placeholder="+7 (999) 000-00-00" disabled={isLoading} className="w-full border border-gray-700 rounded-lg px-4 py-2.5 focus:outline-none focus:ring-2 focus:ring-yellow-400 bg-gray-800 text-white disabled:opacity-50" />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-400 mb-1">Комментарий к реставрации</label>
              <textarea value={userComment} onChange={(e) => setUserComment(e.target.value)} placeholder="Например: убрать царапины, сделать фото цветным" disabled={isLoading} className="w-full border border-gray-700 rounded-lg px-4 py-2.5 focus:outline-none focus:ring-2 focus:ring-yellow-400 bg-gray-800 text-white disabled:opacity-50 resize-none h-24" />
            </div>
            <button onClick={handleSubmitOrder} disabled={isLoading || files.length === 0 || !userName.trim() || !userPhone.trim()} className="w-full mt-2 bg-yellow-400 text-black font-bold py-3.5 px-4 rounded-lg flex items-center justify-center hover:bg-yellow-300 transition-all disabled:bg-gray-500 disabled:text-gray-300 disabled:cursor-not-allowed">
              {isLoading ? ( <><Loader2 className="mr-2 h-5 w-5 animate-spin" /> Отправка...</> ) : ( 'Заказать реставрацию' )}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
import { useState, useCallback, useEffect } from 'react';
import { useDropzone } from 'react-dropzone';
import { UploadCloud, ImageMinus, Download, LoaderCircle } from 'lucide-react';

// Простой компонент для уведомлений (можно заменить на react-hot-toast или аналог)
const Toast = ({ message, show, onClose }) => {
  useEffect(() => {
    if (show) {
      const timer = setTimeout(() => {
        onClose();
      }, 3000);
      return () => clearTimeout(timer);
    }
  }, [show, onClose]);

  if (!show) return null;

  return (
    <div className="fixed top-5 right-5 bg-red-600 text-white py-2 px-4 rounded-lg shadow-lg z-50 animate-fade-in-down">
      {message}
    </div>
  );
};

const RemoveBgWidget = () => {
  const [selectedFile, setSelectedFile] = useState(null);
  const [previewUrl, setPreviewUrl] = useState(null);
  const [resultUrl, setResultUrl] = useState(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    return () => {
      if (previewUrl) {
        URL.revokeObjectURL(previewUrl);
      }
      if (resultUrl && resultUrl.startsWith('blob:')) {
        URL.revokeObjectURL(resultUrl);
      }
    };
  }, [previewUrl, resultUrl]);

  const onDrop = useCallback((acceptedFiles) => {
    // Сбрасываем предыдущий результат при загрузке нового файла
    if (resultUrl) {
        setResultUrl(null);
    }
    const file = acceptedFiles[0];
    if (file) {
      setSelectedFile(file);
      setPreviewUrl(URL.createObjectURL(file));
      setError('');
    }
  }, [resultUrl]);

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    accept: { 'image/*': ['.jpeg', '.jpg', '.png', '.webp'] },
    multiple: false,
  });

  const handleRemoveBg = async () => {
    if (!selectedFile) {
      setError('Пожалуйста, выберите файл.');
      return;
    }

    setIsLoading(true);
    setError('');
    setResultUrl(null);

    const formData = new FormData();
    // Новый бэкенд ожидает ключ 'file'
    formData.append('file', selectedFile);

    try {
      // Адрес должен совпадать с тем, где запущен FastAPI (обычно 8000)
      const response = await fetch('http://127.0.0.1:8000/api/remove-bg', {
        method: 'POST',
        body: formData,
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => null);
        throw new Error(errorData?.detail || 'Бэкенд не отвечает');
      }

      const blob = await response.blob();
      const url = URL.createObjectURL(blob);

      // Устанавливаем результат в стейт
      setResultUrl(url);

    } catch (err) {
      console.error("Ошибка магии:", err);
      setError(err.message || "Магия не сработала. Проверь, запущен ли Python-сервер на порту 8000");
    } finally {
      setIsLoading(false);
    }
  };
  const resetState = () => {
      setSelectedFile(null);
      // previewUrl будет очищен через useEffect
      setPreviewUrl(null);
      setResultUrl(null);
      setError('');
      setIsLoading(false);
  }

  return (
    <div className="w-full max-w-4xl mx-auto p-4 md:p-8 bg-white rounded-xl shadow-lg border border-gray-200">
      <Toast message={error} show={!!error} onClose={() => setError('')} />
      <div className="grid grid-cols-1 md:grid-cols-2 gap-8">
        {/* Левая колонка: Загрузка */}
        <div>
          <h2 className="text-xl font-bold text-gray-800 mb-4">1. Загрузите фото</h2>
          <div
            {...getRootProps()}
            className={`relative border-2 border-dashed rounded-lg p-6 text-center cursor-pointer transition-colors
              ${isDragActive ? 'border-primary bg-primary/10' : 'border-gray-300 hover:border-gray-400'}
              ${previewUrl ? 'flex items-center justify-center h-80' : 'h-80 flex flex-col items-center justify-center'}`}
          >
            <input {...getInputProps()} />
            {previewUrl ? (
              <>
                <img src={previewUrl} alt="Preview" className="max-h-full object-contain rounded-md" />
                <button 
                    onClick={(e) => { e.stopPropagation(); resetState(); }}
                    className="absolute top-2 right-2 bg-red-500 text-white rounded-full p-1.5 shadow-lg hover:bg-red-600 transition-transform transform hover:scale-110"
                    title="Удалить фото"
                >
                    <ImageMinus size={20} />
                </button>
              </>
            ) : (
              <div className="flex flex-col items-center text-gray-500">
                <UploadCloud size={48} className="mb-4 text-gray-400" />
                <p className="font-semibold">Перетащите файл сюда</p>
                <p className="text-sm">или нажмите для выбора</p>
                <p className="text-xs mt-4 text-gray-400">PNG, JPG, WEBP до 15МБ</p>
              </div>
            )}
          </div>
        </div>

        {/* Правая колонка: Результат */}
        <div>
          <h2 className="text-xl font-bold text-gray-800 mb-4">2. Результат</h2>
          <div className="relative w-full h-80 bg-gray-100 rounded-lg flex items-center justify-center 
            bg-[linear-gradient(45deg,#e0e0e0_25%,transparent_25%),linear-gradient(-45deg,#e0e0e0_25%,transparent_25%),linear-gradient(45deg,transparent_75%,#e0e0e0_75%),linear-gradient(-45deg,transparent_75%,#e0e0e0_75%)]
            [background-size:20px_20px] [background-position:0_0,0_10px,10px_-10px,-10px_0px]">
            {isLoading && (
              <div className="flex flex-col items-center text-gray-600">
                <LoaderCircle className="animate-spin" size={48} />
                <p className="mt-4 font-medium">Нейросеть работает...</p>
              </div>
            )}
            {resultUrl && !isLoading && (
              <img src={resultUrl} alt="Result" className="max-h-full max-w-full object-contain" />
            )}
            {!resultUrl && !isLoading && (
                <p className="text-gray-500">Здесь появится результат</p>
            )}
          </div>
        </div>
      </div>
      <div className="mt-6 flex flex-col md:flex-row gap-4">
          <button
            onClick={handleRemoveBg}
            disabled={!selectedFile || isLoading}
            className="w-full md:w-1/2 bg-primary text-primary-foreground font-bold py-3 px-4 rounded-lg flex items-center justify-center gap-2
            hover:bg-primary/90 transition-all disabled:bg-gray-400 disabled:cursor-not-allowed"
          >
            {isLoading ? (
              <>
                <LoaderCircle className="animate-spin" size={20} />
                <span>Обработка...</span>
              </>
            ) : (
              'Удалить фон'
            )}
          </button>
          <a
            href={resultUrl}
            download={`bg-removed-${selectedFile?.name || 'image.png'}`}
            className={`w-full md:w-1/2 bg-green-600 text-white font-bold py-3 px-4 rounded-lg flex items-center justify-center gap-2
            hover:bg-green-700 transition-all ${!resultUrl || isLoading ? 'opacity-50 cursor-not-allowed pointer-events-none' : ''}`}
          >
            <Download size={20} />
            Скачать результат
          </a>
      </div>
    </div>
  );
};

export default RemoveBgWidget;
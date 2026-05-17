import React, { useEffect } from 'react';
import { X } from 'lucide-react';

export default function PrivacyModal({ isOpen, onClose }) {
  useEffect(() => {
    if (!isOpen) return undefined;

    const handleKeyDown = (event) => {
      if (event.key === 'Escape') {
        onClose();
      }
    };

    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [isOpen, onClose]);

  if (!isOpen) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/75 px-4 py-6"
      onMouseDown={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="privacy-modal-title"
        className="relative flex max-h-[88vh] w-full max-w-3xl flex-col rounded-2xl border border-gray-800 bg-gray-950 shadow-2xl"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-4 border-b border-gray-800 px-5 py-4 md:px-6">
          <h2 id="privacy-modal-title" className="text-lg font-bold text-white">
            Политика конфиденциальности
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded-full p-2 text-gray-400 transition-colors hover:bg-gray-900 hover:text-white focus:outline-none focus:ring-2 focus:ring-yellow-400"
            aria-label="Закрыть политику конфиденциальности"
          >
            <X className="h-5 w-5" aria-hidden="true" />
          </button>
        </div>

        <div className="overflow-y-auto px-5 py-5 text-sm leading-7 text-gray-300 md:px-6">
          <div className="space-y-7">
            <section>
              <h2 className="mb-3 text-xl font-bold text-white">
                Политика конфиденциальности сервиса PhotoDoc AI
              </h2>
            </section>

            <section>
              <h2 className="mb-2 text-base font-bold text-white">1. Общие положения</h2>
              <p className="mb-3">
                Настоящая Политика конфиденциальности определяет порядок обработки и защиты
                персональных данных пользователей сервиса PhotoDoc AI (далее — «Сервис»). Мы с
                глубоким уважением относимся к личной информации наших клиентов и строго соблюдаем
                требования законодательства РФ в области защиты персональных данных (ФЗ №152-ФЗ
                «О персональных данных»).
              </p>
              <p>
                Использование Сервиса означает безоговорочное согласие пользователя с настоящей
                Политикой и указанными в ней условиями обработки данных.
              </p>
            </section>

            <section>
              <h2 className="mb-2 text-base font-bold text-white">2. Какие данные мы собираем</h2>
              <p className="mb-3">
                Для качественного оказания услуг мы можем запрашивать следующие данные:
              </p>
              <ul className="list-disc space-y-2 pl-5">
                <li>
                  <strong className="text-gray-100">Контактная информация:</strong> Имя и номер
                  телефона (для связи по вопросам заказа и доставки).
                </li>
                <li>
                  <strong className="text-gray-100">Пользовательские файлы:</strong> Цифровые
                  фотографии, загружаемые клиентом для печати, создания фото на документы или
                  реставрации.
                </li>
                <li>
                  <strong className="text-gray-100">Технические данные:</strong> IP-адрес, данные
                  файлов cookie и метрики браузера (используются Яндекс Метрикой в обезличенном виде
                  для улучшения работы сайта).
                </li>
              </ul>
            </section>

            <section>
              <h2 className="mb-2 text-base font-bold text-white">3. Цели обработки данных</h2>
              <p className="mb-3">Мы используем ваши данные исключительно для:</p>
              <ul className="list-disc space-y-2 pl-5">
                <li>Приема, обработки и выполнения вашего заказа.</li>
                <li>Уведомления о статусе готовности.</li>
                <li>
                  Проведения безопасных онлайн-платежей (через защищенный шлюз Robokassa).
                </li>
                <li>Улучшения качества обслуживания.</li>
              </ul>
            </section>

            <section>
              <h2 className="mb-2 text-base font-bold text-white">
                4. Конфиденциальность фотографий и их удаление
              </h2>
              <p className="mb-3">
                Мы понимаем, насколько важна приватность ваших личных снимков.
              </p>
              <ul className="list-disc space-y-2 pl-5">
                <li>
                  Ваши фотографии используются нашими специалистами и алгоритмами исключительно для
                  выполнения конкретной заказанной услуги (реставрация, замена фона, кадрирование
                  или печать).
                </li>
                <li>
                  <strong className="text-gray-100">Гарантия удаления:</strong> Сразу после
                  успешного выполнения заказа и передачи готовых материалов клиенту, все исходные
                  файлы и обработанные копии безвозвратно удаляются с наших серверов и облачных
                  хранилищ. Мы не используем фотографии клиентов для портфолио, обучения нейросетей
                  или других целей без отдельного, прямого письменного согласия.
                </li>
              </ul>
            </section>

            <section>
              <h2 className="mb-2 text-base font-bold text-white">
                5. Передача данных третьим лицам
              </h2>
              <p className="mb-3">
                Мы не продаем и не передаем ваши персональные данные третьим лицам, за исключением
                случаев, когда это необходимо для выполнения заказа:
              </p>
              <ul className="list-disc space-y-2 pl-5">
                <li>
                  Платежному сервису Robokassa (только данные, необходимые для проведения транзакции
                  и отправки электронного чека).
                </li>
                <li>Службам доставки (если был выбран соответствующий способ получения заказа).</li>
              </ul>
            </section>

            <section>
              <h2 className="mb-2 text-base font-bold text-white">6. Защита информации</h2>
              <p>
                Мы используем современные протоколы шифрования (SSL) для защиты данных при их
                передаче и хранении, предотвращая несанкционированный доступ к вашей информации.
              </p>
            </section>

            <section>
              <h2 className="mb-2 text-base font-bold text-white">7. Контакты для связи</h2>
              <p className="mb-3">
                Если у вас возникли вопросы по поводу удаления данных или работы с вашей информацией,
                вы можете связаться с нами:
              </p>
              <ul className="space-y-2">
                <li>
                  <strong className="text-gray-100">Наименование:</strong> ИНДИВИДУАЛЬНЫЙ
                  ПРЕДПРИНИМАТЕЛЬ ХОДЫРЕВ ОЛЕГ ВЛАДИМИРОВИЧ
                </li>
                <li>
                  <strong className="text-gray-100">ИНН:</strong> 222500647581
                </li>
                <li>
                  <strong className="text-gray-100">Email:</strong> mdot22@yandex.ru
                </li>
              </ul>
            </section>
          </div>
        </div>

        <div className="border-t border-gray-800 px-5 py-4 md:px-6">
          <button
            type="button"
            onClick={onClose}
            className="w-full rounded-xl bg-yellow-400 px-5 py-3 font-bold text-black transition-colors hover:bg-yellow-500 focus:outline-none focus:ring-2 focus:ring-yellow-400 focus:ring-offset-2 focus:ring-offset-gray-950 sm:w-auto"
          >
            Закрыть
          </button>
        </div>
      </div>
    </div>
  );
}

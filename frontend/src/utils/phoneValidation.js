export const PHONE_ERROR_MESSAGE = 'Введите корректный номер телефона (11 цифр)';

export function validateRussianPhone(phone) {
  const justDigits = phone.replace(/\D/g, '');
  return justDigits.length === 11;
}

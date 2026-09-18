export const PHONE_ERROR_MESSAGE = 'Введите корректный номер телефона (11 цифр, начинается с 7 или 8)';

export function validateRussianPhone(phone) {
  const justDigits = phone.replace(/\D/g, '');
  return justDigits.length === 11 && (justDigits[0] === '7' || justDigits[0] === '8');
}

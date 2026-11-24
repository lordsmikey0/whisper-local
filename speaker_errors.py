"""Credential-free preparation diagnostics shared by the isolated worker and server."""
import errno


def preparation_error(error, stage):
    chain, current = [], error
    while current is not None and len(chain) < 8 and all(current is not item for item in chain):
        chain.append(current)
        current = current.__cause__ or current.__context__
    status = next((getattr(getattr(e, 'response', None), 'status_code', None)
                   for e in chain if getattr(getattr(e, 'response', None), 'status_code', None)), None)
    names = {type(e).__name__ for e in chain}
    code = f'{stage}/{status or type(error).__name__}'
    if status == 401 and stage == 'token':
        message = 'Hugging Face отклонил токен: он недействителен, отозван или скопирован не полностью. Создайте токен Read и повторите ввод.'
    elif status in (401, 403) or 'GatedRepoError' in names:
        message = ('Токен не даёт доступа к Community-1. Примите условия модели в том же аккаунте, которому принадлежит токен. '
                   'Для Fine-grained включите разрешение Read access to contents of all public gated repos you can access '
                   'или используйте токен Read.')
    elif status == 429:
        message = 'Hugging Face временно ограничил число запросов. Подождите несколько минут и повторите подготовку.'
    elif status and status >= 500:
        message = 'Ошибка сервера Hugging Face. Повторите подготовку позже.'
    elif any('SSL' in name for name in names):
        message = 'Не удалось проверить сертификат HTTPS. Проверьте дату Windows и сертификаты прокси или антивируса.'
    elif any('Timeout' in name or 'Connection' in name for name in names):
        message = 'Соединение с Hugging Face прервалось или истекло время ожидания. Повторите подготовку; уже скачанные файлы сохраняются.'
    elif any(getattr(e, 'errno', None) == errno.ENOSPC for e in chain):
        message = 'Недостаточно свободного места для модели. Освободите место на диске проекта и повторите подготовку.'
    elif any(isinstance(e, PermissionError) for e in chain):
        message = 'Нет доступа к записи файлов модели. Проверьте права на папку проекта и блокировку файлов антивирусом.'
    elif any(isinstance(e, ImportError) for e in chain):
        message = 'Не удалось загрузить библиотеки говорящих. Повторно запустите setup-speakers.cmd в этой папке проекта.'
    elif stage == 'verify' and 'UnpicklingError' in names:
        message = 'PyTorch заблокировал тип из весов модели. Обновите diarize_worker.py: требуется совместимость с weights_only.'
    elif stage == 'verify':
        message = 'Файлы скачаны, но pyannote не смог загрузить модель локально. Повторите setup-speakers.cmd и подготовку модели.'
    else:
        message = 'Не удалось подготовить модель на указанном этапе. Повторите попытку и сообщите код ошибки.'
    # Never serialize the exception text, request URL, headers or credentials.
    return f'{message} [{code}]'

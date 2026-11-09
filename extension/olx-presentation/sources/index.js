// Список сайтів-джерел. Новий сайт (dom.ria, rieltor.ua…) = новий файл у sources/ з тим самим
// інтерфейсом { id, name, match(url), readMain(), extract(main) } + нормалізатор у lib/.
import { OLX } from "./olx.js";

export const SOURCES = [OLX];

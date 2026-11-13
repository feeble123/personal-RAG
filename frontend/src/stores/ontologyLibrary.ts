/**
 * 当前选中的图谱库（多项目）。
 *
 * 一个库 = 一份 TBox + 属于它的实例与关系；切换库 = 换 `?profile=` 参数。
 * 选择记在 localStorage：图谱页和本体管理页共享同一个"当前库"，
 * 否则从图谱页跳到管理页会莫名其妙切回默认库。
 */
import { create } from 'zustand'

const STORAGE_KEY = 'ontology-library'

function readSaved(): string {
  try {
    return localStorage.getItem(STORAGE_KEY) ?? ''
  } catch {
    return ''
  }
}

interface LibraryState {
  /** 空串 = 用服务端默认库（ONTOLOGY_PROFILE） */
  profileId: string
  setProfileId: (id: string) => void
}

export const useOntologyLibrary = create<LibraryState>()((set) => ({
  profileId: readSaved(),
  setProfileId: (id) => {
    try {
      localStorage.setItem(STORAGE_KEY, id)
    } catch {
      /* 隐私模式下写不进去：只影响"记住上次选择"，不影响功能 */
    }
    set({ profileId: id })
  },
}))

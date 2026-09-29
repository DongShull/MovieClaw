/**
 * 进度上报带的轨选择（服务端的轨记忆，docs/design/jellyfin-subtitle.md §3.3）。
 *
 * 记忆只记**用户亲手选的**：播放器自己挑的轨（服务端默认挑选、为了省转码换过去的
 * 同语言轨、没有默认字幕所以没开）不报——报了就会被当成用户的选择存下，以后默认
 * 策略怎么改这部片都跟不上，自动落成的「关闭」还会连带整部剧都不再开字幕。
 * 不报（undefined）时服务端保持原记忆不动，所以上一次选过、这次没动的也不会丢。
 *
 * 服务端还会再筛一遍：报上来的就是这个文件的默认挑选时同样不记（用户特意选回默认，
 * 清空记忆和记住它效果一样）。`file_id` 让服务端在多版本时对准正在放的那个版本。
 */
export interface ReportedTracks {
  audio_track?: string;
  subtitle_track?: string;
  file_id?: number;
}

export function reportedTracks(input: {
  /** 用户在音轨菜单里点选的轨；没点过为 null */
  requestedAudio: string | null;
  /** 用户这次动过字幕菜单没有 */
  subtitleTouched: boolean;
  /** 当前选中的字幕；null = 没开 */
  selectedSubtitle: string | null;
  /** 会话决策里正在放的文件 */
  fileId: number | null | undefined;
}): ReportedTracks {
  const tracks: ReportedTracks = {};
  if (input.requestedAudio) tracks.audio_track = input.requestedAudio;
  if (input.subtitleTouched) tracks.subtitle_track = input.selectedSubtitle ?? "off";
  if (input.fileId != null) tracks.file_id = input.fileId;
  return tracks;
}

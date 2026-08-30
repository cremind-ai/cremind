/**
 * Who a room post belongs to, from the viewer's point of view.
 *
 * Shared rather than inlined because two surfaces have to agree: the bubble
 * decides which side of the timeline to sit on, and the view aligns the routing
 * chip that annotates it. If those two ever disagreed, a post would render on
 * one side with its footnote under the other.
 */

import type { GroupMessage } from '../../services/groupChatApi';

/**
 * Whether this post is the viewer's own — a message they typed in the room's
 * composer, as opposed to a peer operator's or an agent's.
 *
 * A web post carries the poster's profile as its sender id, which is what makes
 * this per-viewer rather than a property of the row. An admin speaking through
 * `as_profile` is written as an AGENT post and is deliberately not "own": it
 * reads in the room as that agent talking, and it should read that way here.
 */
export function isOwnWebPost(
  message: GroupMessage, viewerProfile: string,
): boolean {
  if (!viewerProfile || message.sender_kind !== 'user') return false;
  const identity = message.sender_identity;
  return identity?.channel_type === 'web' && identity?.sender_id === viewerProfile;
}

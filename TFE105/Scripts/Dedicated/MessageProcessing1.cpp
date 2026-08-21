/* Copyright (c) 2022-2026 Dreamy Cecil
This program is free software; you can redistribute it and/or modify
it under the terms of version 2 of the GNU General Public License as published by
the Free Software Foundation


This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License along
with this program; if not, write to the Free Software Foundation, Inc.,
51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA. */

#include "StdH.h"

#include "MessageProcessing.h"
#include "NetworkFunctions.h"
#include "Modules.h"

#include "Query/QueryManager.h"

#include "Query/PlayerDB.h" // 1111
#include "Query/GeoIP.h"    // 1111

// Join event hook. If cmd_cmdOnJoin is non-empty, it's executed as a shell
// script every time a player joins. Before it runs, cmd_strJoinName/
// cmd_strJoinIP/cmd_iJoinClient/cmd_iJoinPlayer are filled in with that
// player's info so the script can read them; cmd_iChatClient is the
// equivalent "current client" context used by the chat hook below.  1111
static CTString cmd_cmdOnJoin = "";
static CTString cmd_strJoinName = "";
static CTString cmd_strJoinIP = "";
static INDEX    cmd_iJoinClient = -1;
static INDEX    cmd_iJoinPlayer = -1;
static INDEX    cmd_iChatClient = -1;

// Chat event hook. If cmd_cmdOnChat is non-empty, it's executed as a shell
// script every time a chat message is sent; cmd_strChatMessage holds that
// message for the duration of the call (cleared again right after).
// cmd_strSilentChatCommands is a ';'-separated list of exact messages
// (e.g. custom chat commands) that should never be broadcast to other
// players, only trigger cmd_cmdOnChat.    1111
static CTString cmd_cmdOnChat = "";
static CTString cmd_strChatMessage = "";
static CTString cmd_strSilentChatCommands = "";  // ';'-separated exact matches that never get broadcast
static CTString cmd_strPMCmd = "@pm";   //  1111 configurable PM command trigger

// Team chat: lets players message only others who share their
// "Team" player-setting (the free-text field under the nickname), without
// it ever reaching the public chat or opposing team.	1111
static CTString cmd_strTeamCmd = "@tm";
static CTString cmd_cmdOnTeamChat = "";

// General-purpose scratch strings for scripts (init.ini / round scripts).
// Not read or written by any C++ code - they exist purely so scripts can
// stash a value (a snippet to hand to ScheduleScript, an entity ID, etc.)
// in a persistent-for-the-session shell variable instead of a local one.  1111
static CTString cmd_strFirstExtra = "";
static CTString cmd_strSecondExtra = "";
static CTString cmd_strThirdExtra = "";
static CTString cmd_strFourthExtra = "";
static CTString cmd_strFifthExtra = "";
static CTString cmd_strSixthExtra = "";
static CTString cmd_strSeventhExtra = "";
static CTString cmd_strEighthExtra = "";

// Which client sent last packet to the server
INDEX IProcessPacket::_iHandlingClient = IProcessPacket::CLT_NONE;

// Notify clients whenever they desync
INDEX IProcessPacket::_bReportSyncBadToClients = FALSE;

// Prevent clients from joining unless they have the same patch installed
INDEX IProcessPacket::_bForbidVanilla = FALSE;

#if _PATCHCONFIG_GAMEPLAY_EXT

// Gameplay extensions (reset to recommended settings)
IConfig::GameplayExt IProcessPacket::_gexSetup(FALSE);

// Check if gameplay extensions are enabled for the server
BOOL IProcessPacket::GameplayExtEnabled(void) {
  return _gexSetup[k_EGameplayExt_Enable].IsTrue();
};

#else

// Gameplay extensions don't exist
BOOL IProcessPacket::GameplayExtEnabled(void) {
  return FALSE;
};

#endif // _PATCHCONFIG_GAMEPLAY_EXT

// Allow changing value of a symbol unless currently running a server
BOOL IProcessPacket::UpdateSymbolValue(void *pSymbol) {
  // Cannot change the value while running the game as a server
  if (_pNetwork->IsServer()) {
    CPutString(TRANS("Cannot change the value of this shell symbol while the server is running!\n"));
    return FALSE;
  }

  // Safe to change
  return TRUE;
};

// Register commands for packet processing
void IProcessPacket::RegisterCommands(void) {
  _pShell->DeclareSymbol("INDEX UpdateServerSymbolValue(INDEX);", &UpdateSymbolValue);

  _pShell->DeclareSymbol("persistent user INDEX ser_bReportSyncBadToClients;", &_bReportSyncBadToClients);
  _pShell->DeclareSymbol("persistent user INDEX ser_bForbidVanilla pre:UpdateServerSymbolValue;", &_bForbidVanilla);

  // Join event script and context variables    1111
  _pShell->DeclareSymbol("persistent user CTString cmd_cmdOnJoin;", &cmd_cmdOnJoin);
  _pShell->DeclareSymbol("user CTString cmd_strJoinName;", &cmd_strJoinName);
  _pShell->DeclareSymbol("user CTString cmd_strJoinIP;", &cmd_strJoinIP);
  _pShell->DeclareSymbol("user INDEX    cmd_iJoinClient;", &cmd_iJoinClient);
  _pShell->DeclareSymbol("user INDEX    cmd_iJoinPlayer;", &cmd_iJoinPlayer);
  _pShell->DeclareSymbol("user INDEX cmd_iChatClient;", &cmd_iChatClient);

  // Chat event script and context variables    1111
  _pShell->DeclareSymbol("persistent user CTString cmd_cmdOnChat;", &cmd_cmdOnChat);
  _pShell->DeclareSymbol("user CTString cmd_strChatMessage;", &cmd_strChatMessage);
  _pShell->DeclareSymbol("persistent user CTString cmd_strSilentChatCommands;", &cmd_strSilentChatCommands);
  _pShell->DeclareSymbol("persistent user CTString cmd_strPMCmd;", &cmd_strPMCmd);

  // Extra strings    1111
  _pShell->DeclareSymbol("user CTString cmd_strFirstExtra;", &cmd_strFirstExtra);
  _pShell->DeclareSymbol("user CTString cmd_strSecondExtra;", &cmd_strSecondExtra);
  _pShell->DeclareSymbol("user CTString cmd_strThirdExtra;", &cmd_strThirdExtra);
  _pShell->DeclareSymbol("user CTString cmd_strFourthExtra;", &cmd_strFourthExtra);
  _pShell->DeclareSymbol("user CTString cmd_strFifthExtra;", &cmd_strFifthExtra);
  _pShell->DeclareSymbol("user CTString cmd_strSixthExtra;", &cmd_strSixthExtra);
  _pShell->DeclareSymbol("user CTString cmd_strSeventhExtra;", &cmd_strSeventhExtra);
  _pShell->DeclareSymbol("user CTString cmd_strEighthExtra;", &cmd_strEighthExtra);

#if _PATCHCONFIG_GAMEPLAY_EXT
  // Gameplay extensions
  _gexSetup.DeclareSymbols();
#endif
};

#if _PATCHCONFIG_GUID_MASKING

// Arrays of sync checks per client
CStaticArray<IProcessPacket::CSyncCheckArray> IProcessPacket::_aClientChecks;

// Should mask player GUIDs or not
BOOL IProcessPacket::_bMaskGUIDs = FALSE;

// Check if should use GUID masking
BOOL IProcessPacket::ShouldMaskGUIDs(void) {
  // Setting is on; multiplayer game
  return IProcessPacket::_bMaskGUIDs && INetwork::IsHostingMultiplayer();
};

// Clear arrays with sync checks
void IProcessPacket::ClearSyncChecks(void)
{
  FOREACHINSTATICARRAY(IProcessPacket::_aClientChecks, IProcessPacket::CSyncCheckArray, itar) {
    itar->Clear();
  }
};

#endif // _PATCHCONFIG_GUID_MASKING

// Buffer sync check for the server
void IProcessPacket::AddSyncCheck(const INDEX iClient, const CSyncCheck &sc)
{
#if _PATCHCONFIG_GUID_MASKING
  // Use the first array if not masking
  CSyncCheckArray &aChecks = _aClientChecks[ShouldMaskGUIDs() ? iClient : 0];
#else
  CSyncCheckArray &aChecks = _pNetwork->ga_srvServer.srv_ascChecks;
#endif

  // Recreate the buffer if the size differs
  static CSymbolPtr symptr("ser_iSyncCheckBuffer");
  INDEX &iBuffer = symptr.GetIndex();

  iBuffer = ClampDn(iBuffer, (INDEX)1);

  if (aChecks.Count() != iBuffer) {
    aChecks.Clear();
    aChecks.New(iBuffer);
  }

  // Find the oldest one
  INDEX iOldest = 0;

  for (INDEX i = 1; i < aChecks.Count(); i++) {
    if (aChecks[i].sc_tmTick < aChecks[iOldest].sc_tmTick) {
      iOldest = i;
    }
  }

  // Overwrite it
  aChecks[iOldest] = sc;
};

// Find buffered sync check for a given tick
INDEX IProcessPacket::FindSyncCheck(const INDEX iClient, TIME tmTick, CSyncCheck &sc)
{
#if _PATCHCONFIG_GUID_MASKING
  // Use the first array if not masking
  CSyncCheckArray &aChecks = _aClientChecks[ShouldMaskGUIDs() ? iClient : 0];
#else
  CSyncCheckArray &aChecks = _pNetwork->ga_srvServer.srv_ascChecks;
#endif

  BOOL bHasEarlier = FALSE;
  BOOL bHasLater = FALSE;

  for (INDEX i = 0; i < aChecks.Count(); i++) {
    TIME tmInTable = aChecks[i].sc_tmTick;

    if (tmInTable == tmTick) {
      sc = aChecks[i];
      return 0;

    } else if (tmInTable < tmTick) {
      bHasEarlier = TRUE;

    } else if (tmInTable > tmTick) {
      bHasLater = TRUE;
    }
  }

  if (!bHasEarlier) {
    ASSERT(bHasLater);
    return -1;

  } else if (!bHasLater) {
    ASSERT(bHasEarlier);
    return +1;
  }

  // Cannot have earlier, later and not found all at once
  ASSERT(FALSE);
  return +1;
};

// Mask player GUID using data from the player buffer
void IProcessPacket::MaskGUID(UBYTE *aubGUID, CPlayerBuffer &plb) {
  // Clear GUID
  memset(aubGUID, 0, 16);

  // Use client and buffer indices for uniqueness
  aubGUID[0] = plb.plb_iClient;
  aubGUID[1] = plb.plb_Index;
};

// Check if character can be changed for a specific player
BOOL IProcessPacket::CanChangeCharacter(CPlayerEntity *pen) {
  // Retrieve CPlayer::m_penAnimator
  static CPropertyPtr pptrAnimator(pen);

  // Check if player has a player animator to handle the character change
  if (pptrAnimator.ByVariable("CPlayer", "m_penAnimator"))
  {
    CEntity *penAnimator = ENTITYPROPERTY(pen, pptrAnimator.Offset(), CEntityPointer);
    return (penAnimator != NULL);
  }

  // Just proceed if cannot determine
  return TRUE;
};

// Client confirming the disconnection
void IProcessPacket::OnClientDisconnect(INDEX iClient, CNetworkMessage &nmMessage) {
  CSessionSocket &sso = _pNetwork->ga_srvServer.srv_assoSessions[iClient];
  sso.sso_iDisconnectedState = 2;

  // Make client inactive
  CActiveClient::DeactivateClient(iClient);
};

// Try checking if the client is running the same patch version as the server
static BOOL CheckClientPatch(INDEX iClient, CNetworkMessage &nmMessage) {
  const CTString strClient = GetComm().Server_GetClientName(iClient);

  // Tag length and client version
  const ULONG ctTagLen = sizeof(_aSessionStatePatchTag);
  ULONG ulClientVer;

  // Desired position in the packet vs the last possible position
  const UBYTE *pubDesired = nmMessage.nm_pubPointer + ctTagLen + sizeof(ulClientVer);
  const UBYTE *pubEnd = nmMessage.nm_pubMessage + nmMessage.nm_slSize;

  // If there's enough space for the tag and the version
  if (pubDesired <= pubEnd) {
    // Read them
    char aClientTag[ctTagLen];

    nmMessage.Read(aClientTag, ctTagLen);
    nmMessage >> ulClientVer;

    // Verify client tag and version
    const BOOL bTagMatch = (memcmp(aClientTag, _aSessionStatePatchTag, ctTagLen) == 0);
    const BOOL bVersionMatch = (ulClientVer == ClassicsCore_GetVersion());

    CPrintF(TRANS("Server: Client '%s' has provided an identification tag:\n"
                  "  Tag match: %d | Version match: %d\n"), strClient.str_String, bTagMatch, bVersionMatch);

    // Client is running the right patch
    return (bTagMatch && bVersionMatch);

  } else {
    CPrintF(TRANS("Server: Client '%s' hasn't provided an identification tag\n"), strClient.str_String);
  }

  // Client isn't running the right patch
  return FALSE;
};

// Client requesting the session state
void IProcessPacket::OnConnectRemoteSessionStateRequest(INDEX iClient, CNetworkMessage &nmMessage)
{
  // [Cecil] Get identity of a remote client
  ASSERT(iClient > 0);
  CClientIdentity *pci = IClientLogging::GetIdentity(iClient);

  // [Cecil] Check if the client is banned
  CClientRestriction &cr = pci->crRestrictions;
  const BOOL bBanned = cr.IsBanned();

  static CSymbolPtr pbWhiteList("ser_bInverseBanning");

  // Not allowed on the server
  if (bBanned == !pbWhiteList.GetIndex()) {
    // No specific ban record
    if (!bBanned) {
      INetwork::SendDisconnectMessage(iClient, TRANS("\n^cff1111YOU ARE NOT ALLOWED ON THIS SERVER!^r"), TRUE);
      return;
    }

    CTString strTime, strReason;
    cr.PrintBanTime(strTime);

    strReason.PrintF(TRANS("\n^cff1111YOU ARE BANNED! ^c00ff80%s left^r"), strTime);
    INetwork::SendDisconnectMessage(iClient, strReason, TRUE);
    return;
  }

  // Original function code
  static CSymbolPtr pstrIPMask("ser_strIPMask");

  // IP address is banned
  if (IData::MatchesMask(GetComm().Server_GetClientName(iClient), pstrIPMask.GetString()) == !pbWhiteList.GetIndex()) {
    INetwork::SendDisconnectMessage(iClient, LOCALIZE("\n^cff1111YOU ARE BANNED FROM THIS SERVER^r"), TRUE);
    return;
  }

  // Default version info
  INDEX iMajor = 109;
  INDEX iMinor = 1;

  // Read version tag
  INDEX iTag;
  nmMessage >> iTag;

  if (iTag == 'VTAG') {
    nmMessage >> iMajor >> iMinor;
  }

  // Disconnect if mismatching version
  if (iMajor != _SE_BUILD_MAJOR || iMinor != _SE_BUILD_MINOR) {
    CTString strExplanation;
    strExplanation.PrintF(LOCALIZE(
      "This server runs version %d.%d, your version is %d.%d.\n"
      "Please visit http://www.croteam.com for information on version updating."),
      _SE_BUILD_MAJOR, _SE_BUILD_MINOR, iMajor, iMinor);

    INetwork::SendDisconnectMessage(iClient, strExplanation, TRUE);
    return;
  }

  // Check mod
  CTString strGivenMod;
  nmMessage >> strGivenMod;

  // Disconnect with a special non-translatable mod tag
  if (_strModName != strGivenMod) {
    CTString strMod(0, "MOD:%s\\%s", _strModName, _strModURL);
    INetwork::SendDisconnectMessage(iClient, strMod, TRUE);
    return;
  }

  CTString strGivenPassword;
  nmMessage >> strGivenPassword;

  INDEX ctWantedLocalPlayers;
  nmMessage >> ctWantedLocalPlayers;

  // [1111] Reject a negative/absurd local-player count outright. Without this,
  // a negative value here sails past CheckSplitScreenClients() (which only
  // checks the upper bound) and can make ctCurrentPlayers+ctWantedLocalPlayers
  // in the "server full" check below wrap negative, silently bypassing the
  // player-count/session-slot limit for this connecting client.
  if (ctWantedLocalPlayers < 0 || ctWantedLocalPlayers > ICore::MAX_GAME_PLAYERS) {
    INetwork::SendDisconnectMessage(iClient, LOCALIZE("Protocol violation"), TRUE);
    return;
  }
  
  // [Cecil] Read socket parameters in advance
  CSessionSocketParams sspClient;
  nmMessage >> sspClient;

  // [Cecil] Check if vanilla clients are forbidden or using incompatible gameplay extensions
  const BOOL bForbid = (_bForbidVanilla || GameplayExtEnabled());

  // [Cecil] Disconnect unless the client has the right patch version installed
  if (bForbid && !CheckClientPatch(iClient, nmMessage)) {
    // Prompt to download the right patch version
    const CTString strVer = ClassicsCore_GetVersionName();
    const CTString strMod = "MOD:Classics Patch " + strVer + "\\" + CLASSICSPATCH_URL_TAGRELEASE(strVer);

    INetwork::SendDisconnectMessage(iClient, strMod, TRUE);
    return;
  }

  // [Cecil] Check for connecting clients with split-screen
  if (!CheckSplitScreenClients(iClient, ctWantedLocalPlayers)) return;

  static CSymbolPtr pstrConnectPassword("net_strConnectPassword");
  static CSymbolPtr pstrVIPPassword("net_strVIPPassword");
  static CSymbolPtr pstrObserverPassword("net_strObserverPassword");
  static CSymbolPtr piVIPReserve("net_iVIPReserve");
  static CSymbolPtr piMaxObservers("net_iMaxObservers");
  static CSymbolPtr piMaxClients("net_iMaxClients");

  const CTString strPwdConnect  = pstrConnectPassword.GetString();
  const CTString strPwdVIP      = pstrVIPPassword.GetString();
  const CTString strPwdObserver = pstrObserverPassword.GetString();

  // Count allowed players, clients and VIPs, then check if allowed to connect
  INDEX ctMaxAllowedPlayers = _pNetwork->ga_sesSessionState.ses_ctMaxPlayers;
  INDEX ctMaxAllowedClients = ctMaxAllowedPlayers;

  // [1111] The engine's own CSessionState::Start_t() briefly resets
  // ses_ctMaxPlayers to exactly 1 while a new session is starting - this runs
  // *after* the socket is already listening (CNetworkLibrary::StartPeerToPeer_t
  // starts the server before applying the real player-count config), so there's
  // a real window, right after a map-change/restart, where the server is
  // reachable but effectively capped at 1 player. A second (or later) client
  // reconnecting in that window sees a flat "Server full!" even though the
  // server is actually empty and about to allow far more players. gam_ctMaxPlayers
  // holds the real configured cap, so if the live value is suspiciously stuck at
  // 1 while the real config wants more, tell the truth instead.
  INDEX ctMaxPlayers = _pNetwork->ga_sesSessionState.ses_ctMaxPlayers;

  if (ctMaxAllowedPlayers <= 1 && ctMaxPlayers > 1) {
    INetwork::SendDisconnectMessage(iClient, LOCALIZE("Server is still starting up, please try reconnecting in a few seconds"), TRUE);
    return;
  }

  if (piMaxClients.GetIndex() > 0) {
    ctMaxAllowedClients = ClampUp(piMaxClients.GetIndex(), ICore::MAX_GAME_COMPUTERS);
  }

  INDEX ctMaxAllowedVIPPlayers = 0;
  INDEX ctMaxAllowedVIPClients = 0;

  if (piVIPReserve.GetIndex() > 0 && strPwdVIP != "") {
    ctMaxAllowedVIPPlayers = ClampDn(piVIPReserve.GetIndex() - INetwork::CountPlayers(TRUE), 0L);
    ctMaxAllowedVIPClients = ClampDn(piVIPReserve.GetIndex() - INetwork::CountClients(TRUE), 0L);
  }

  const INDEX ctMaxAllowedObservers = piMaxObservers.GetIndex();

  // Get current amounts
  const INDEX ctCurrentPlayers = INetwork::CountPlayers(FALSE);
  const INDEX ctCurrentClients = INetwork::CountClients(FALSE);
  const INDEX ctCurrentObservers = INetwork::CountObservers();

  // Check which passwords this client can satisfy
  BOOL bAutorizedAsVIP = FALSE;
  BOOL bAutorizedAsObserver = FALSE;
  BOOL bAutorizedAsPlayer = FALSE;

  if (strPwdVIP != "" && strPwdVIP == strGivenPassword) {
    bAutorizedAsVIP = TRUE;
    bAutorizedAsPlayer = TRUE;
    bAutorizedAsObserver = TRUE;
  }

  if (strPwdConnect == "" || strPwdConnect == strGivenPassword) {
    bAutorizedAsPlayer = TRUE;
  }

  if ((strPwdObserver == "" && bAutorizedAsPlayer) || strPwdObserver == strGivenPassword) {
    bAutorizedAsObserver = TRUE;
  }

  // Artificially decrease allowed number of players and clients for VIPs
  if (!bAutorizedAsVIP) {
    ctMaxAllowedPlayers = ClampDn(ctMaxAllowedPlayers - ctMaxAllowedVIPPlayers, 0L);
    ctMaxAllowedClients = ClampDn(ctMaxAllowedClients - ctMaxAllowedVIPClients, 0L);
  }

  // Disconnect if too many clients or players
  if (ctCurrentPlayers + ctWantedLocalPlayers > ctMaxAllowedPlayers
   || ctCurrentClients + 1 > ctMaxAllowedClients) {
    INetwork::SendDisconnectMessage(iClient, LOCALIZE("Server full!"), TRUE);
    return;
  }

  // Disconnect observers
  if (ctWantedLocalPlayers == 0) {
    // If too many
    if (ctCurrentObservers >= ctMaxAllowedObservers && !bAutorizedAsVIP) {
      INetwork::SendDisconnectMessage(iClient, LOCALIZE("Too many observers!"), TRUE);
      return;
    }

    // If password is wrong
    if (!bAutorizedAsObserver) {
      if (strGivenPassword == "") {
        INetwork::SendDisconnectMessage(iClient, LOCALIZE("This server requires password for observers!"), TRUE);
      } else {
        INetwork::SendDisconnectMessage(iClient, LOCALIZE("Wrong observer password!"), TRUE);
      }
      return;
    }

  // Disconnect players if password is wrong
  } else if (!bAutorizedAsPlayer) {
    if (strGivenPassword == "") {
      INetwork::SendDisconnectMessage(iClient, LOCALIZE("This server requires password to connect!"), TRUE);
    } else {
      INetwork::SendDisconnectMessage(iClient, LOCALIZE("Wrong password!"), TRUE);
    }
    return;
  }

  // Activate client socket and read parameters for it
  CSessionSocket &sso = _pNetwork->ga_srvServer.srv_assoSessions[iClient];
  sso.Activate();

  sso.sso_ctLocalPlayers = ctWantedLocalPlayers;
  sso.sso_bVIP = bAutorizedAsVIP;
  sso.sso_sspParams = sspClient;

  // Try to send base info
  try {
    static CSymbolPtr pstrMOTD("ser_strMOTD");

    CTMemoryStream strmInfo;
    strmInfo << INDEX(MSG_REP_CONNECTREMOTESESSIONSTATE);
    strmInfo << pstrMOTD.GetString();
    strmInfo << _pNetwork->ga_World.wo_fnmFileName;
    strmInfo << _pNetwork->ga_sesSessionState.ses_ulSpawnFlags;
    strmInfo.Write_t(_pNetwork->ga_aubDefaultProperties, NET_MAXSESSIONPROPERTIES);

    // [Cecil] Append extra info to the session state for patched clients
    WriteServerInfoToSessionState(strmInfo);

    const SLONG slSize = strmInfo.GetStreamSize() >> 10; // Size in KB

    // Send reply to the remote session state
    _pNetwork->SendToClientReliable(iClient, strmInfo);

    CPrintF(LOCALIZE("Server: Sent initialization info to '%s' (%dk)\n"),
      GetComm().Server_GetClientName(iClient).str_String, slSize);

  // Abort on error
  } catch (char *strError) {
    sso.Deactivate();
    CPrintF(LOCALIZE("Server: Cannot prepare connection data: %s\n"), strError);
  }
};

// Client requesting the connection to the server
void IProcessPacket::OnPlayerConnectRequest(INDEX iClient, CNetworkMessage &nmMessage)
{
  // Read character data
  CPlayerCharacter pcCharacter;
  nmMessage >> pcCharacter;

  // Get client identity
  CClientIdentity *pci = IClientLogging::GetIdentity(iClient);

  // Add new character to the identity
  pci->AddNewCharacter(pcCharacter);

  // Check for blacklisted/whitelisted character names
  if (!GetComm().Server_IsClientLocal(iClient)) {
    static CSymbolPtr pstrNameMask("ser_strNameMask");
    static CSymbolPtr pbWhiteList("ser_bInverseBanning");

    // Character name is banned
    if (IData::MatchesMask(pcCharacter.GetName(), pstrNameMask.GetString()) == !pbWhiteList.GetIndex()) {
      INetwork::SendDisconnectMessage(iClient, LOCALIZE("\n^cff1111YOU ARE BANNED FROM THIS SERVER^r"), FALSE);
      return;
    }

    // -- 1111: check SQLite bans table -------------------------------------
    {
        char szBanGUID[33];
        PlayerDB_FormatGUID(pcCharacter.pc_aubGUID, szBanGUID);

        SClientAddress banAddr;
        IClientLogging::GetAddress(banAddr, iClient);
        ULONG ulBanIP = banAddr.GetIP();
        char szBanIP[16];
        wsprintfA(szBanIP, "%u.%u.%u.%u",
            (ulBanIP >> 24) & 0xFF, (ulBanIP >> 16) & 0xFF,
            (ulBanIP >> 8) & 0xFF, ulBanIP & 0xFF);

        CTString strBanReason = PlayerDB_CheckBan(szBanGUID, szBanIP);
        if (strBanReason != "") {
            INetwork::SendDisconnectMessage(iClient, strBanReason, FALSE);
            return;
        }
    }
    // -- end 1111 ----------------------------------------------------------
  }

  CServer &srv = _pNetwork->ga_srvServer;
  CSessionSocket &sso = srv.srv_assoSessions[iClient];

  // Check if someone's connecting with too many players
  if (iClient > 0 && INetwork::CountClientPlayers(iClient) >= sso.sso_ctLocalPlayers) {
    INetwork::SendDisconnectMessage(iClient, LOCALIZE("Protocol violation"), FALSE);
    return;
  }

  // If there's a used character already
  if (INetwork::IsCharacterUsed(pcCharacter)) {
    // Refuse connection
    CTString strMessage;
    strMessage.PrintF(LOCALIZE("Player character '%s' already exists in this session."), pcCharacter.GetName());

    INetwork::SendDisconnectMessage(iClient, strMessage, FALSE);
    return;
  }

  // Find inactive player for the client
  CPlayerBuffer *pplbNew = INetwork::FirstInactivePlayer();

  // If able to add new players
  if (pplbNew != NULL) {
    // Activate new player
    pplbNew->Activate(iClient);
    INDEX iNewPlayer = pplbNew->plb_Index;

    // Let plugins handle characters
    FOREACHPLUGIN(itPlugin) {
      if (itPlugin->pm_events.m_packet->OnCharacterConnect == NULL) continue;

      itPlugin->pm_events.m_packet->OnCharacterConnect(iClient, pcCharacter);
    }

    // Remember the character
    pplbNew->plb_pcCharacter = pcCharacter;

    const INDEX iLastSequence = ++srv.srv_iLastProcessedSequence;

  #if _PATCHCONFIG_GUID_MASKING
    if (ShouldMaskGUIDs()) {
      // Send message back to this client about adding a new player
      if (iClient == 0 || sso.sso_bActive) {
        CNetStreamBlock nsbClientPlayer(MSG_SEQ_ADDPLAYER, iLastSequence);
        nsbClientPlayer << iNewPlayer;
        nsbClientPlayer << pcCharacter;

        INetwork::AddBlockToSession(nsbClientPlayer, iClient);
      }

      // Mask player GUID for other clients
      MaskGUID(pcCharacter.pc_aubGUID, *pplbNew);
    }
  #endif // _PATCHCONFIG_GUID_MASKING

    // Send message to other clients about adding a new player
    CNetStreamBlock nsbAddClientData(MSG_SEQ_ADDPLAYER, iLastSequence);
    nsbAddClientData << iNewPlayer;
    nsbAddClientData << pcCharacter;

  #if _PATCHCONFIG_GUID_MASKING
    // Send to other clients
    if (ShouldMaskGUIDs()) {
      for (INDEX iSession = 0; iSession < srv.srv_assoSessions.Count(); iSession++) {
        CSessionSocket &ssoBlock = srv.srv_assoSessions[iSession];

        if (iSession == iClient || (iSession > 0 && !ssoBlock.IsActive())) {
          continue;
        }

        INetwork::AddBlockToSession(nsbAddClientData, iSession);
      }

    } else
  #endif // _PATCHCONFIG_GUID_MASKING
    {
      // Send to everyone
      INetwork::AddBlockToAllSessions(nsbAddClientData);
    }

    // Don't wait for any more players
    _pShell->Execute("ser_bWaitFirstPlayer = 0;");

    // Reply to this client about adding a new player
    CNetworkMessage nmPlayerRegistered(MSG_REP_CONNECTPLAYER);
    nmPlayerRegistered << iNewPlayer;

    _pNetwork->SendToClientReliable(iClient, nmPlayerRegistered);

    // Add new player to the active client
    CActiveClient &acClient = _aActiveClients[iClient];
    ASSERT(acClient.IsActive() && acClient.pClient == pci);

    acClient.AddPlayer(pplbNew);

    // Record player in stats DB    1111
    {
        SClientAddress addr;
        IClientLogging::GetAddress(addr, iClient);
        ULONG ulIP = addr.GetIP();
        char szIP[16];
        wsprintfA(szIP, "%u.%u.%u.%u",
            (ulIP >> 24) & 0xFF, (ulIP >> 16) & 0xFF,
            (ulIP >> 8) & 0xFF, ulIP & 0xFF);

        PlayerDB_OnJoin(iNewPlayer, pcCharacter.pc_aubGUID,
            pcCharacter.GetName(), szIP);

        GeoIP_Lookup(szIP);

        // [Cecil] Fire player join script event    1111
        if (cmd_cmdOnJoin != "") {
            cmd_strJoinName = CTString(pcCharacter.GetName());
            cmd_strJoinIP = szIP;
            cmd_iJoinClient = (INDEX)iClient;
            cmd_iJoinPlayer = (INDEX)iNewPlayer;

            _pShell->Execute(cmd_cmdOnJoin + "\n");

            cmd_strJoinName = "";
            cmd_strJoinIP = "";
            cmd_iJoinClient = -1;
            cmd_iJoinPlayer = -1;
        }
    }

    // Notify master server that a player is connecting
    static CSymbolPtr symptr("ser_bEnumeration");

    if (symptr.GetIndex()) {
      IMasterServer::OnServerStateChanged();
    }

  // If too many players
  } else {
    // Refuse connection
    INetwork::SendDisconnectMessage(iClient, LOCALIZE("Too many players in session."), FALSE);
  }
};

// Client changing the character
void IProcessPacket::OnCharacterChangeRequest(INDEX iClient, CNetworkMessage &nmMessage)
{
  // Read character data
  INDEX iPlayer;
  CPlayerCharacter pcCharacter;
  nmMessage >> iPlayer >> pcCharacter;

  // Get client identity
  CClientIdentity *pci = IClientLogging::GetIdentity(iClient);

  // Add new character to the identity
  pci->AddNewCharacter(pcCharacter);

  // Skip character changes blocked by the anti-flood system
  if (IAntiFlood::HandleCharacterChange(iClient)) {
    return;
  }

  CServer &srv = _pNetwork->ga_srvServer;

  // Invalid player
  // [1111] fixed off-by-one: valid indices are 0..Count()-1, so iPlayer ==
  // Count() must also be rejected. This bound was ">" (inherited from the
  // original engine), letting iPlayer == Count() through and dereferencing
  // one CPlayerBuffer past the end of srv_aplbPlayers below - undefined
  // behavior on every release build, since CStaticArray::operator[]'s bounds
  // check is an ASSERT (compiled out in release).
  if (iPlayer < 0 || iPlayer >= srv.srv_aplbPlayers.Count() ) {
    return;
  }

  CPlayerBuffer &plb = srv.srv_aplbPlayers[iPlayer];

  // Wrong client or character
  if (plb.plb_iClient != iClient || !(plb.plb_pcCharacter == pcCharacter)) {
    return;
  }

  // [Cecil] Check if the entity is even capable of changing its appearance
  CPlayerEntity *penPlayer = _pNetwork->ga_sesSessionState.ses_apltPlayers[iPlayer].plt_penPlayerEntity;

  if (!CanChangeCharacter(penPlayer)) {
    INetwork::SendChatToClient(iClient, LOCALIZE("Server"), TRANS("Please wait until you are fully connected to change your character."));
    return;
  }

  // Let plugins handle characters
  FOREACHPLUGIN(itPlugin) {
    if (itPlugin->pm_events.m_packet->OnCharacterChange == NULL) continue;

    // Quit if cannot change the character
    if (!itPlugin->pm_events.m_packet->OnCharacterChange(iClient, iPlayer, pcCharacter)) {
      return;
    }
  }

  // Remember the character
  plb.plb_pcCharacter = pcCharacter;

  const INDEX iLastSequence = ++srv.srv_iLastProcessedSequence;

#if _PATCHCONFIG_GUID_MASKING
  if (ShouldMaskGUIDs()) {
    // Send character change back to this client
    if (iClient == 0 || srv.srv_assoSessions[iClient].sso_bActive) {
      CNetStreamBlock nsbClientChar(MSG_SEQ_CHARACTERCHANGE, iLastSequence);
      nsbClientChar << iPlayer;
      nsbClientChar << pcCharacter;

      INetwork::AddBlockToSession(nsbClientChar, iClient);
    }

    // Mask player GUID for other clients
    MaskGUID(pcCharacter.pc_aubGUID, plb);
  }
#endif // _PATCHCONFIG_GUID_MASKING

  // Send character change to other clients
  CNetStreamBlock nsbChangeChar(MSG_SEQ_CHARACTERCHANGE, iLastSequence);
  nsbChangeChar << iPlayer;
  nsbChangeChar << pcCharacter;

#if _PATCHCONFIG_GUID_MASKING
  // Send to other clients
  if (ShouldMaskGUIDs()) {
    for (INDEX iSession = 0; iSession < srv.srv_assoSessions.Count(); iSession++) {
      CSessionSocket &ssoBlock = srv.srv_assoSessions[iSession];

      if (iSession == iClient || (iSession > 0 && !ssoBlock.IsActive())) {
        continue;
      }

      INetwork::AddBlockToSession(nsbChangeChar, iSession);
    }

  } else
#endif // _PATCHCONFIG_GUID_MASKING
  {
    // Send to everyone
    INetwork::AddBlockToAllSessions(nsbChangeChar);
  }
};

// Receive action packet from one player of a client
static void ReceiveActionsForPlayer(INDEX iClient, INDEX iPlayer, CNetworkMessage &nm, INDEX iMaxBuffer)
{
  CPlayerBuffer &plb = _pNetwork->ga_srvServer.srv_aplbPlayers[iPlayer];
  ASSERT(plb.plb_Active);

  // Receive new action
  CPlayerAction pa;
  nm >> pa;

  // Let plugins handle actions
  FOREACHPLUGIN(itPlugin) {
    if (itPlugin->pm_events.m_packet->OnPlayerAction == NULL) continue;

    itPlugin->pm_events.m_packet->OnPlayerAction(iClient, iPlayer, pa, -1);
  }

  // Buffer it
  plb.plb_abReceived.AddAction(pa);

  INDEX iSendBehind = 0;
  nm.ReadBits(&iSendBehind, 2);

  // Add resent actions
  for (INDEX i = 0; i < iSendBehind; i++) {
    CPlayerAction paOld;
    nm >> paOld;

    // Let plugins handle actions
    FOREACHPLUGIN(itPlugin) {
      if (itPlugin->pm_events.m_packet->OnPlayerAction == NULL) continue;

      itPlugin->pm_events.m_packet->OnPlayerAction(iClient, iPlayer, pa, i);
    }

    if (paOld.pa_llCreated > plb.plb_paLastAction.pa_llCreated) {
      plb.plb_abReceived.AddAction(paOld);
    }
  }

  // If there are too many actions buffered
  while (plb.plb_abReceived.GetCount() > iMaxBuffer) {
    // Purge the oldest one
    plb.plb_abReceived.RemoveOldest();
  }
};

// Client sending player actions
void IProcessPacket::OnPlayerAction(INDEX iClient, CNetworkMessage &nmMessage)
{
  CServer &srv = _pNetwork->ga_srvServer;
  CSessionSocket &sso = srv.srv_assoSessions[iClient];

  // For each possible player on that client
  for (INDEX i = 0; i < ICore::MAX_LOCAL_PLAYERS; i++) {
    // [Cecil] Stop reading actions if there are less than 4 bytes of data left
    // 15 bits / 2 bytes for bSaved, iPlayer and plb_iPing and then CPlayerAction (more than 2 extra bytes)
    const INDEX ctBytesLeft = nmMessage.nm_slSize - INDEX(nmMessage.nm_pubPointer - nmMessage.nm_pubMessage);
    if (ctBytesLeft < 4) break;

    // See if saved in the message
    BOOL bSaved = 0;
    nmMessage.ReadBits(&bSaved, 1);

    if (!bSaved) continue;

    // Read client index
    INDEX iPlayer = 0;
    nmMessage.ReadBits(&iPlayer, 4);

    CPlayerBuffer &plb = srv.srv_aplbPlayers[iPlayer];

    // If there is no player on that client
    if (plb.plb_iClient != iClient) {
      // Consider the entire message invalid
      CPrintF("Wrong Client! (expected: %d, got: %d from srv_aplbPlayers[%d])\n", iClient, plb.plb_iClient, iPlayer);
      break;
    }

    // Read ping
    plb.plb_iPing = 0;
    nmMessage.ReadBits(&plb.plb_iPing, 10);

    // Let the corresponding client buffer receive the message
    INDEX iMaxBuffer = sso.sso_sspParams.ssp_iBufferActions;

    static CSymbolPtr symptr("cli_bPredictIfServer");

    if (iClient == 0 && !symptr.GetIndex()) {
      iMaxBuffer = 1;
    }

    ReceiveActionsForPlayer(iClient, iPlayer, nmMessage, iMaxBuffer);
  }
};

// Client sends a CRC check
void IProcessPacket::OnSyncCheck(INDEX iClient, CNetworkMessage &nmMessage) {
  static CSymbolPtr pbReportSyncOK("ser_bReportSyncOK");
  static CSymbolPtr pbReportSyncBad("ser_bReportSyncBad");
  static CSymbolPtr pbReportSyncLate("ser_bReportSyncLate");
  static CSymbolPtr pbReportSyncEarly("ser_bReportSyncEarly");
  static CSymbolPtr pbPauseOnSyncBad("ser_bPauseOnSyncBad");
  static CSymbolPtr piKickOnSyncBad("ser_iKickOnSyncBad");

  CServer &srv = _pNetwork->ga_srvServer;

  // Read sync check from the packet
  CSyncCheck scRemote;
  nmMessage.Read(&scRemote, sizeof(scRemote));

  const TIME tmTick = scRemote.sc_tmTick;

  // Try to find it in the buffer
  CSyncCheck scLocal;
  INDEX iFound = FindSyncCheck(iClient, tmTick, scLocal);

  CSessionSocket &sso = srv.srv_assoSessions[iClient];
  TIME &tmLastSync = sso.sso_tmLastSyncReceived;

  const CTString strClientName = GetComm().Server_GetClientName(iClient);

  // Sync on time
  if (iFound == 0) {
    // Flush stream buffer up to that sequence
    ((CNetStream &)sso.sso_nsBuffer).RemoveOlderBlocksBySequence(scRemote.sc_iSequence);

    // Disconnect if the level has changed
    if (scLocal.sc_iLevel != scRemote.sc_iLevel) {
      INetwork::SendDisconnectMessage(iClient, LOCALIZE("Level change in progress. Please retry."), FALSE);

    // Wrong CRC
    } else if (scLocal.sc_ulCRC != scRemote.sc_ulCRC) {
      sso.sso_ctBadSyncs++;

      if (pbReportSyncBad.GetIndex()) {
        CTString strSyncBad;
        strSyncBad.PrintF(LOCALIZE("SYNCBAD: Client '%s', Sequence %d Tick %.2f - bad %d\n"),
          strClientName, scRemote.sc_iSequence, tmTick, sso.sso_ctBadSyncs);

        CPutString(strSyncBad);

        // [Cecil] Report bad syncs to the client
        if (_bReportSyncBadToClients && (sso.sso_ctBadSyncs % 10 == 0)) {   //  Added mod 10 statement  1111
          strSyncBad.PrintF(TRANS("^CSequence %d Tick %.2f - ^cff2e2ebad %d^r"),
            scRemote.sc_iSequence, tmTick, sso.sso_ctBadSyncs);

          INetwork::SendChatToClient(iClient, LOCALIZE("^cff2e2eSYNCBAD"), strSyncBad);
        }
      }

      // Kick from too many bad sync
      if (piKickOnSyncBad.GetIndex() > 0) {
        if (sso.sso_ctBadSyncs >= piKickOnSyncBad.GetIndex()) {
          INetwork::SendDisconnectMessage(iClient, LOCALIZE("\n^cf8f644TOO MANY BAD SYNC^r"), FALSE);
        }

      // Pause on any bad sync
      } else if (pbPauseOnSyncBad.GetIndex()) {
        _pNetwork->ga_sesSessionState.ses_bWantPause = TRUE;
      }

    // Clear bad syncs
    } else {
      sso.sso_ctBadSyncs = 0;

      if (pbReportSyncOK.GetIndex()) {
        CPrintF(LOCALIZE("SYNCOK: Client '%s', Tick %.2f\n"), strClientName, tmTick);
      }
    }

    // Remember that this sync is for this tick
    if (tmLastSync < tmTick) tmLastSync = tmTick;

  // Too old
  } else if (iFound < 0) {
    // Only report if syncs are okay (to avoid late syncs on level change)
    if (pbReportSyncLate.GetIndex() && tmLastSync > 0) {
      CPrintF(LOCALIZE("SYNCLATE: Client '%s', Tick %.2f\n"), strClientName, tmTick);
    }

  // Too new
  } else {
    if (pbReportSyncEarly.GetIndex()) {
      CPrintF(LOCALIZE("SYNCEARLY: Client '%s', Tick %.2f\n"), strClientName, tmTick);
    }

    // Remember that this sync was ahead of time
    if (tmLastSync < tmTick) tmLastSync = tmTick;
  }
};

// Must be outside the function to link to the global variable in NetworkFunctions.cpp  1111
extern CTString ser_strChatName;

// Exact-match lookup against cmd_strSilentChatCommands (';'-separated).
// This decision is made WITHOUT running any script, so it never depends on
// (or gets delayed by) script output -- keeps message ordering correct.   1111
static BOOL IsSilentChatCommand(const CTString& strMsg) {
    if (cmd_strSilentChatCommands == "" || strMsg == "") return FALSE;

    char szList[512];
    strncpy(szList, cmd_strSilentChatCommands.str_String, sizeof(szList) - 1);
    szList[sizeof(szList) - 1] = '\0';

    char* pToken = strtok(szList, ";");

    while (pToken != NULL) {
        if (strMsg == CTString(pToken)) return TRUE;
        pToken = strtok(NULL, ";");
    }
    return FALSE;
}

/* ---- ResolveClientSlot ------------------------------------------------- *
 * Accepts a pure-decimal session slot index (from @list) OR a partial,      *
 * case-insensitive, decoration-stripped player name fragment.                *
 * Sends bilingual error feedback to iRequester automatically.                *
 * Returns slot >= 1 on success, -1 on failure.                               *
 * ----------------------------------------------------------------------- */
 static void SendClientInfo(INDEX iClient, const CTString &strEN, const CTString &strRU) {  /* 1111 */
    INetwork::SendChatToClient(iClient, "^CServer", " ");
    INetwork::SendChatToClient(iClient, "^ced2675", SelectLangMessage(iClient, strEN, strRU));
}

static INDEX ResolveClientSlot(const CTString& strArg, INDEX iRequester) {  /* 1111 */
    if (!_pNetwork->IsServer()) return -1;
    CServer& srv = _pNetwork->ga_srvServer;

    // Declare loop variables at the top to avoid MSVC for-scope leaking
    INDEX iSearchSlot, ipl;
    char* pLower;

    // Numeric: only if every character is a digit
    BOOL bAllDigits = (strArg.str_String[0] != '\0');
    {
        const char* p = strArg.str_String;
        while (*p) {
            if (*p < '0' || *p > '9') { bAllDigits = FALSE; break; }
            p++;
        }
    }

    if (bAllDigits) {
        INDEX iSlot = -1;
        sscanf(strArg.str_String, "%d", &iSlot);   // sscanf: no const issue
        if (iSlot >= 1 && iSlot < (INDEX)srv.srv_assoSessions.Count()
            && srv.srv_assoSessions[iSlot].sso_bActive) {
            return iSlot;
        }
        SendClientInfo(iRequester,
			"^c00ff80Slot not found - use ^cefefef@list^c00ff80 for active players",
			"^c00ff80Слот не найден - используйте ^cefefef@list");
        return -1;
    }

    // Name search: lowercase the query 
    char szQuery[256];
    memset(szQuery, 0, sizeof(szQuery));
    strncpy(szQuery, strArg.str_String, sizeof(szQuery) - 1);
    for (pLower = szQuery; *pLower; pLower++) *pLower = (char)tolower((UBYTE)*pLower);

    INDEX iFound = -1;
    INDEX ctFound = 0;

    for (iSearchSlot = 1; iSearchSlot < (INDEX)srv.srv_assoSessions.Count(); iSearchSlot++) {
        if (!srv.srv_assoSessions[iSearchSlot].sso_bActive) continue;

        CTString strName = "";
        for (ipl = 0; ipl < (INDEX)srv.srv_aplbPlayers.Count(); ipl++) {
            CPlayerBuffer& plb = srv.srv_aplbPlayers[ipl];
            if (!plb.IsActive() || plb.plb_iClient != iSearchSlot) continue;
            strName = plb.plb_pcCharacter.GetName().Undecorated();
            break;
        }
        if (strName == "") continue;

        char szName[256];
        memset(szName, 0, sizeof(szName));
        strncpy(szName, strName.str_String, sizeof(szName) - 1);
        for (pLower = szName; *pLower; pLower++) *pLower = (char)tolower((UBYTE)*pLower);

        if (strstr(szName, szQuery) != NULL) {
            iFound = iSearchSlot;
            ctFound++;
        }
    }

    if (ctFound == 1) return iFound;

    if (ctFound == 0) {
        SendClientInfo(iRequester,
			"^c00ff80No player matches - try ^cefefef@list",
			"^c00ff80Игрок не найден - попробуйте ^cefefef@list");
        return -1;
    }

    // ambiguous — header first, then one line per matching slot  /* 1111 */
    INetwork::SendChatToClient(iRequester, "^CServer", " ");
    INetwork::SendChatToClient(iRequester, "^ced2675",
        SelectLangMessage(iRequester,
            "^c00ff80Multiple matches - be more specific:",
            "^c00ff80Несколько совпадений - уточните имя:"));

    for (iSearchSlot = 1; iSearchSlot < (INDEX)srv.srv_assoSessions.Count(); iSearchSlot++) {
        if (!srv.srv_assoSessions[iSearchSlot].sso_bActive) continue;
        CTString strName = "";
        for (ipl = 0; ipl < (INDEX)srv.srv_aplbPlayers.Count(); ipl++) {
            CPlayerBuffer &plb = srv.srv_aplbPlayers[ipl];
            if (!plb.IsActive() || plb.plb_iClient != iSearchSlot) continue;
            strName = plb.plb_pcCharacter.GetName().Undecorated();
            break;
        }
        char szName[256];
        memset(szName, 0, sizeof(szName));
        strncpy(szName, strName.str_String, sizeof(szName) - 1);
        for (pLower = szName; *pLower; pLower++) *pLower = (char)tolower((UBYTE)*pLower);
        if (strstr(szName, szQuery) == NULL) continue;

        CTString strLine;
        strLine.PrintF("^cffff90%d ^c888888- ^cffda59%s", (int)iSearchSlot, strName.str_String);
        INetwork::SendChatToClient(iRequester, "^ced2675", strLine);
    }
    return -1;
}

/* ---- HandlePMCommand --------------------------------------------------- *
 * Handles the @pm <client> <message> private message command.               *
 * Both sender and recipient receive the same packet; the original chat      *
 * message is suppressed (returns FALSE). Never reaches the broadcast path. *
 * ----------------------------------------------------------------------- */
static BOOL HandlePMCommand(INDEX iClient, const CTString& strMessage) {  /* 1111 */
    CTString      strArgs;
    CTString      strTargetArg;
    CTString      strPMText;
    CTString      strSenderName;
    CTString      strFromField;
    CTString      strMsgField;
    const char* pSplit;
    char          szTarget[256];
    INDEX         iTargetLen;
    INDEX         iTarget;
    INDEX         ipl;

    strArgs = strMessage;
    strArgs.RemovePrefix(cmd_strPMCmd);
    strArgs.TrimSpacesLeft();

    pSplit = strArgs.str_String;
    while (*pSplit && *pSplit != ' ') pSplit++;

    if (strArgs == "" || *pSplit == '\0') {
        SendClientInfo(iClient,
			"^c00ff80Usage: ^cefefef@pm <name or slot#> <message>",
			"^c00ff80Использование: ^cefefef@pm <имя или слот> <сообщение>");
		return FALSE;
    }

    memset(szTarget, 0, sizeof(szTarget));
    iTargetLen = (INDEX)(pSplit - strArgs.str_String);
    if (iTargetLen > 255) iTargetLen = 255;
    memcpy(szTarget, strArgs.str_String, iTargetLen);
    strTargetArg = szTarget;

    while (*pSplit == ' ') pSplit++;
    if (*pSplit == '\0') {
        SendClientInfo(iClient,
			"^c00ff80No message provided",
			"^c00ff80Сообщение не указано");
        return FALSE;
    }
    strPMText = pSplit;

    iTarget = ResolveClientSlot(strTargetArg, iClient);
    if (iTarget < 0) return FALSE;

    strSenderName = "Server";
    CServer& srv = _pNetwork->ga_srvServer;
    for (ipl = 0; ipl < (INDEX)srv.srv_aplbPlayers.Count(); ipl++) {
        CPlayerBuffer& plb = srv.srv_aplbPlayers[ipl];
        if (!plb.IsActive() || plb.plb_iClient != iClient) continue;
        strSenderName = ColorizeNick_Impl(plb.plb_pcCharacter.GetName());
        break;
    }

    // Recipient and sender may have different languages, so build tag per-slot
    CTString strFromRecipient = SelectLangMessage(iTarget,
        "[private message] ", "[личное сообщение] ")
        + strSenderName;
    CTString strFromSender = SelectLangMessage(iClient,
        "[private message] ", "[личное сообщение] ")
        + strSenderName;

    INetwork::SendChatToClient(iTarget, strFromRecipient, strPMText);
    INetwork::SendChatToClient(iClient, strFromSender,    strPMText);
    return FALSE;
}

static BOOL HandleKickCommand(INDEX iClient, const CTString& strMessage) {  /* 1111 */
    CTString strArg = strMessage;
    strArg.RemovePrefix("@kick");
    strArg.TrimSpacesLeft();

    INDEX iTarget = ResolveClientSlot(strArg, iClient);
    if (iTarget < 0) return FALSE;

    CTString strExec;
    strExec.PrintF("VoteForKick(%d, %d);", (int)iClient, (int)iTarget);
    _pShell->Execute(strExec);
    return FALSE;
}

static BOOL HandleBanCommand(INDEX iClient, const CTString& strMessage) {  /* 1111 */
    CTString strArg = strMessage;
    strArg.RemovePrefix("@ban");
    strArg.TrimSpacesLeft();

    INDEX iTarget = ResolveClientSlot(strArg, iClient);
    if (iTarget < 0) return FALSE;

    CTString strExec;
    strExec.PrintF("VoteForBan(%d, %d);", (int)iClient, (int)iTarget);
    _pShell->Execute(strExec);
    return FALSE;
}

static BOOL HandleBrowseCommand(INDEX iClient) {    /* 1111 */
    CTString strExec;
    strExec.PrintF("BrowsePublicServers(%d);", (int)iClient);
    _pShell->Execute(strExec);
    return FALSE;
}

/* ---- GetTeamForClient --------------------------------------------------- *
 * Reads the free-text "Team" player-setting (shown under the nickname) for
 * a currently-connected client. Returns "" if the client isn't found or
 * hasn't set one -- an empty team is treated as "no team" everywhere below.
 * ------------------------------------------------------------------------- */
static CTString GetTeamForClient(INDEX iClient) {  /* 1111 */
    if (!_pNetwork->IsServer()) return CTString("");
 
    CServer& srv = _pNetwork->ga_srvServer;
    for (INDEX ipl = 0; ipl < (INDEX)srv.srv_aplbPlayers.Count(); ipl++) {
        CPlayerBuffer& plb = srv.srv_aplbPlayers[ipl];
        if (!plb.IsActive() || plb.plb_iClient != iClient) continue;
        return plb.plb_pcCharacter.GetTeam();
    }
    return CTString("");
}
 
/* ---- HandleTeamCommand --------------------------------------------------- *
 * "@tm <message>" -- delivered only to other connected clients whose
 * Team setting exactly matches the sender's. Never touches public chat, so
 * opponents in a team deathmatch never see it, the same way HandlePMCommand
 * never lets a private message leak into the public channel.
 * ------------------------------------------------------------------------- */
static BOOL HandleTeamCommand(INDEX iClient, const CTString& strMessage) {  /* 1111 */
    CTString strMsg = strMessage;
    strMsg.RemovePrefix(cmd_strTeamCmd);
    strMsg.TrimSpacesLeft();
 
    if (strMsg == "") {
        SendClientInfo(iClient,
            "^c00ff80Usage: ^cefefef@tm <message>",
            "^c00ff80Использование: ^cefefef@tm <message>");
        return FALSE;
    }
 
    CTString strMyTeam = GetTeamForClient(iClient);
    if (strMyTeam == "") {
        SendClientInfo(iClient,
            "^c00ff80You're not on a team - set one in your player settings first",
            "^c00ff80У вас нет команды - укажите её в настройках игрока");
        return FALSE;
    }
 
    CTString strSenderName = "Server";
    CServer& srv = _pNetwork->ga_srvServer;
    for (INDEX ipl = 0; ipl < (INDEX)srv.srv_aplbPlayers.Count(); ipl++) {
        CPlayerBuffer& plb = srv.srv_aplbPlayers[ipl];
        if (!plb.IsActive() || plb.plb_iClient != iClient) continue;
        strSenderName = ColorizeNick_Impl(plb.plb_pcCharacter.GetName());
        break;
    }
 
    const INDEX ctSessions = srv.srv_assoSessions.Count();
 
    for (INDEX i = 1; i < ctSessions; i++) {
        if (!srv.srv_assoSessions[i].sso_bActive) continue;
        if (GetTeamForClient(i) != strMyTeam) continue;
		
		CTString strFrom;
        strFrom.PrintF("[%s^r] %s", strMyTeam, strSenderName);
        INetwork::SendChatToClient(i, strFrom, strMsg);
    }
 
    // [Cecil] Extra user-defined script hook -- runs in addition to the team
    // delivery above, never instead of it.    1111
    if (cmd_cmdOnTeamChat != "") {
        cmd_strChatMessage = strMessage;
        _pShell->Execute(cmd_cmdOnTeamChat + "\n");
        cmd_strChatMessage = "";
    }
    return FALSE;
}
 
// Client sending a chat message
BOOL IProcessPacket::OnChatInRequest(INDEX iClient, CNetworkMessage &nmMessage)
{
  // Skip messages blocked by the anti-flood system
  if (IAntiFlood::HandleChatMessage(iClient)) {
    return FALSE;
  }

  ULONG ulFrom, ulTo;
  CTString strMessage;

  nmMessage >> ulFrom >> ulTo >> strMessage;
  nmMessage.Rewind();

  // Let plugins handle chat messages
  FOREACHPLUGIN(itPlugin) {
    if (itPlugin->pm_events.m_packet->OnChatMessage == NULL) continue;

    // Quit if it's not a regular chat message
    if (!itPlugin->pm_events.m_packet->OnChatMessage(iClient, ulFrom, ulTo, strMessage)) {
      return FALSE;
    }
  }

  // Handle chat command if the message starts with a command prefix
  if (strMessage.HasPrefix(ser_strCommandPrefix)) {
      // Intercept @pm before the generic command router    1111
      CTString strCheck = strMessage;
      strCheck.RemovePrefix(ser_strCommandPrefix);
      if (strCheck.HasPrefix("pm ") || strCheck == "pm") {
          return HandlePMCommand(iClient, strMessage);
      }
      return HandleChatCommand(iClient, strMessage);    
  }

  // ==== Hijack chat routing ====  1111

  cmd_iChatClient = iClient;

  // Silent commands (e.g. "@en") never get broadcast at all -- run
  // the reaction script now and return; there's no original message for
  // the response to be out of order with.    1111
  if (IsSilentChatCommand(strMessage)) {
      if (cmd_cmdOnChat != "") {
          cmd_strChatMessage = strMessage;
          _pShell->Execute(cmd_cmdOnChat + "\n");
          cmd_strChatMessage = "";
      }
      return FALSE;
  }

  CServer& srv = _pNetwork->ga_srvServer;

  // --- REIMPLEMENTED MaskOfPlayersOnClient ---
  // Create a bitmask of players that belong to this client
  ULONG ulClientPlayers = 0;
  for (INDEX ipl = 0; ipl < srv.srv_aplbPlayers.Count(); ipl++) {
      CPlayerBuffer& plb = srv.srv_aplbPlayers[ipl];
      if (plb.IsActive() && plb.plb_iClient == iClient) {
          ulClientPlayers |= (1UL << ipl);
      }
  }
  // -------------------------------------------

  // Filter the from address by the client's players
  ulFrom &= ulClientPlayers;
  if (ulFrom == 0) {
      ulTo = -1; // Public message
  }

  // -- PM / TM / kick / ban - must intercept before nmOut is built  	1111
  if (cmd_strPMCmd != "" &&
      (strMessage.HasPrefix(cmd_strPMCmd + " ") || strMessage == cmd_strPMCmd)) {
      return HandlePMCommand(iClient, strMessage);
  }
  if (cmd_strTeamCmd != "" &&
      (strMessage.HasPrefix(cmd_strTeamCmd + " ") || strMessage == cmd_strTeamCmd)) {
      return HandleTeamCommand(iClient, strMessage);
  }
  if (strMessage.HasPrefix("@kick ")) { return HandleKickCommand(iClient, strMessage); }
  if (strMessage.HasPrefix("@ban "))  { return HandleBanCommand (iClient, strMessage); }
  if (strMessage.HasPrefix("@players")) { HandleBrowseCommand(iClient); }

  // Resolve and colorize sender name for player chat   1111
  CTString strColorizedSender = "";
  if (ulFrom != 0) {
      for (INDEX ipl = 0; ipl < srv.srv_aplbPlayers.Count(); ipl++) {
          if (!(ulFrom & (1UL << ipl))) continue;
          CPlayerBuffer& plb = srv.srv_aplbPlayers[ipl];
          if (plb.IsActive()) {
              strColorizedSender = ColorizeNick_Impl(plb.plb_pcCharacter.GetName());
          }
          break;
      }
  }

  // Create custom outgoing chat message
  CNetworkMessage nmOut(MSG_CHAT_OUT);
  // was: nmOut << ulFrom;
  nmOut << (INDEX)0;    //  1111

  if (ulFrom == 0) {
      CTString strFrom;
      if (iClient == 0) {
          // Use our custom variable, with a fallback if the user clears the string 1111
          strFrom = (ser_strChatName != "") ? ser_strChatName : TRANS("Server");
      }
      else {
          strFrom.PrintF(TRANS("Client %d"), iClient);
      }
      nmOut << strFrom;
  } 
  else {
      // Player: use the colorized name resolved above  1111
      nmOut << strColorizedSender;
  }

  nmOut << strMessage;

  // Broadcast to all relevant clients
  for (INDEX iSession = 0; iSession < srv.srv_assoSessions.Count(); iSession++) {
      CSessionSocket& sso = srv.srv_assoSessions[iSession];
      if (iSession > 0 && !sso.IsActive()) continue;

      // --- REIMPLEMENTED MaskOfPlayersOnClient (for destination check) ---
      ULONG ulDestPlayers = 0;
      for (INDEX ipl = 0; ipl < srv.srv_aplbPlayers.Count(); ipl++) {
          CPlayerBuffer& plb = srv.srv_aplbPlayers[ipl];
          if (plb.IsActive() && plb.plb_iClient == iSession) {
              ulDestPlayers |= (1UL << ipl);
          }
      }
      // -------------------------------------------------------------------

      if (ulTo == -1 || (ulTo & ulDestPlayers)) {
          _pNetwork->SendToClient(iSession, nmOut);
      }
  }

  // Fire chat script event AFTER the original message has already
  // gone out, so any reply the script sends is always ordered after it   1111
  if (cmd_cmdOnChat != "") {
      cmd_strChatMessage = strMessage;
      _pShell->Execute(cmd_cmdOnChat + "\n");
      cmd_strChatMessage = "";
  }

  // Return FALSE so the vanilla engine doesn't double-process the packet
  return FALSE;
};


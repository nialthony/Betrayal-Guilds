// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract EntryGate {
    uint256 public immutable entryFeeWei;
    uint256 public immutable ttlSeconds;

    // prevent replay on-chain (agentId + sessionKey)
    mapping(bytes32 => mapping(bytes32 => bool)) public used;

    event EntryPaid(
        bytes32 indexed agentId,
        bytes32 indexed sessionKey,
        address indexed payer,
        uint256 amountWei,
        uint256 expiresAt
    );

    constructor(uint256 _entryFeeWei, uint256 _ttlSeconds) {
        entryFeeWei = _entryFeeWei;
        ttlSeconds = _ttlSeconds;
    }

    function payEntry(bytes32 agentId, bytes32 sessionKey) external payable {
        require(msg.value >= entryFeeWei, "insufficient fee");
        require(!used[agentId][sessionKey], "session used");
        used[agentId][sessionKey] = true;

        uint256 expiresAt = block.timestamp + ttlSeconds;
        emit EntryPaid(agentId, sessionKey, msg.sender, msg.value, expiresAt);
    }
}

import pytest
from brownie import GTC, TokenDistributor, Timelock, accounts, web3, Wei, reverts, chain
import brownie
import time
import requests
import json
import hmac
import hashlib
import binascii
import csv
import random
import os
import sys
from dotenv import dotenv_values

#load up envars from .env
env = dotenv_values(".tests-env-2")
try: 
    test = env['TIMELOCK_DELAY']
except Exception as e:
    print(f'There was an issue getting envars from file. Please check that file exists and values are correct format: {e}')
    sys.exit(1)

# confirm we can hit the ESMS
try:
    esms_response = requests.get(env['V1_API_URL'])
    # we should get a 405 from GET request to POST only endpoint 
    if (esms_response.status_code != '405'):
        raise Exception("ESMS does not appear to be responding to requests") 
    print(f'Response from ESMS: {esms_response.status_code}')
except Exception as e:
    print(f'Some tests will fail - {e}') 

@pytest.fixture(scope="module")
def token():
    '''
        Deploy the token contract w/three params:
        multiSig - The initial account to grant all the tokens
        minter_ - The account with minting ability 
        mintingAllowedAfter_ - The timestamp after which minting may occur (unix time)
    '''
    multiSig = accounts[0]
    minter = accounts[0]
    mintingAllowedAfter = int(time.time()) 
    return GTC.deploy(multiSig, minter, mintingAllowedAfter, {'from': accounts[0]})

@pytest.fixture(scope="module")
def tl():
    '''
        TimeLock Contract - Only needed here in the TD test as all GTC not claimed 
        can be swept to TimeLock after 6 months
    '''
    multiSig = accounts[0]
    return Timelock.deploy(multiSig, env['TIMELOCK_DELAY'], {'from': accounts[0]})

@pytest.fixture(scope="module")
def td(token, tl):
    '''
        TokenDistributor.sol constructor params:
        <constructor(address _token, address _signer, address _timeLock, bytes32 _merkleRoot)> 
        _token - ERC20 token that will be distributed  
        _signer - pub-key/address of the account used to sign token claims
        _timeLock - Address for the TimeLock contract
        _merkleRoot - Merkle Root of the distribution tree 
    '''
    _token = token.address
    return TokenDistributor.deploy(_token, env['SIGNING_ADDRESS'], tl.address, env['MERKLE_ROOT'], {'from': accounts[0]})

@pytest.fixture(scope="module")
def set_dist_address(token, td):
    '''Token needs to know the tokenDist contract address for approved setting of delegate with different source address'''
    return token.setGTCDist(td.address, {'from': accounts[0]}) 

@pytest.fixture(scope="module")
def seed(token, td):
    '''Tansfer seed tokens to the distributor contract'''
    return token.transfer(td.address, Wei('1000000 ether'), {'from': accounts[0]})

@pytest.fixture(autouse=True)
def isolation(fn_isolation):
    '''snapshot/isolate the env after above fixtures so the tests below run against a clean snapshot'''
    pass

def test_valid_contract_address(token, td):
    '''generic test to confirm we have a working contract address'''
    assert web3.isChecksumAddress(token.address) and web3.isChecksumAddress(token.address), "One or more contract addresses could not be validated. Please confirm contracts we're deployed as expected."
 
def test_dist_address_on_token(token, td):
    token.setGTCDist(td.address, {'from': accounts[0]})
    assert token.GTCDist() != '0x0000000000000000000000000000000000000000', "Token doesn't have the TokenDistribution contract address set appropriately for delegation on dist."

def test_valid_claim(token,td,seed,set_dist_address): 
    '''
       Submit claim to ESMS use respone to make on-chain claim.
       Test that a valid claim will transfer tokens to user  
    '''

    valid_claim = ValidClaim() # get known valid claim base metadata 
    token_claim = TokenClaim(valid_claim.user_id, valid_claim.claim_address, valid_claim.delegate_address, valid_claim.total_claim) # get signed token claim from the EMSM
    
    # get use balance before claim 
    balance_before = token.balanceOf(valid_claim.claim_address)

    # place token claim 
    td.claimTokens(token_claim.user_id, token_claim.user_address, token_claim.user_amount, token_claim.delegate_address, token_claim.hash, token_claim.sig, token_claim.proof, token_claim.leaf, {'from' : token_claim.user_address})

    # get use balance before claim 
    balance_after = token.balanceOf(token_claim.user_address)

    print(f'balance_before: {balance_before}')
    print(f'balance_after: {balance_after}')

    assert balance_before < balance_after, "Tokens do not appear to have been sent"
    # uncomment to debug and print details to stdout 
    # assert False, "You intentionally triggered execpetion to print debug info to stdout"

def test_claim_from_different_source(token,td,set_dist_address):
    '''
    The token distribution contract is designed to only allow a claim to proceed if the 
    msg.sender address matches the user_account address provided in the signed message object.
    ''' 
    valid_claim = ValidClaim() # get known valid claim base metadata 
    token_claim = TokenClaim(valid_claim.user_id, valid_claim.claim_address, valid_claim.delegate_address, valid_claim.total_claim) # get signed token claim from the EMSM
    
    # should revert as we're sending claim from different msg.sender 
    with brownie.reverts("TokenDistributor: Must be msg sender."):
        td.claimTokens(token_claim.user_id, token_claim.user_address, token_claim.user_amount, token_claim.delegate_address, token_claim.hash, token_claim.sig, token_claim.proof, token_claim.leaf, {'from' : accounts[2].address})

def test_only_claim_once(token,td,set_dist_address):
    '''
    Token distribution contract is designed to only allow a given user (as per the initial_dist.csv)
    claim tokens exactly one time. Should revert if user attempts to claim twice
    '''
    valid_claim = ValidClaim() # get known valid claim base metadata 
    token_claim = TokenClaim(valid_claim.user_id, valid_claim.claim_address, valid_claim.delegate_address, valid_claim.total_claim) # get signed token claim from the EMSM
    
    # should be successful claim 
    td.claimTokens(token_claim.user_id, token_claim.user_address, token_claim.user_amount, token_claim.delegate_address, token_claim.hash, token_claim.sig, token_claim.proof, token_claim.leaf, {'from' : token_claim.user_address}
    )

    # should revert as we're trying to claim again for the same user  
    with brownie.reverts("TokenDistributor: Tokens already claimed."):
        td.claimTokens(token_claim.user_id, token_claim.user_address, token_claim.user_amount, token_claim.delegate_address, token_claim.hash, token_claim.sig, token_claim.proof, token_claim.leaf, {'from' : token_claim.user_address})
  
def test_claim_fails_with_bad_metadata(token,td,set_dist_address):
    '''
    In addition to checking if a claim is signed by the expected acccount
    we also confirm that key metadata provided with the claim can 
    be re-hashed to create the original message that was signed. 
    This helps ensure integrity of the claim. 
    '''
    valid_claim = ValidClaim() # get known valid claim base metadata 
    token_claim = TokenClaim(valid_claim.user_id, valid_claim.claim_address, valid_claim.delegate_address, valid_claim.total_claim) # get signed token claim from the EMSM
    
    # should revert as we send a different claim amount than that of which was provided with the original message 
    with brownie.reverts("TokenDistributor: Claim Hash Mismatch."):
        td.claimTokens(token_claim.user_id, token_claim.user_address, 1223943873000000061440, token_claim.delegate_address, token_claim.hash, token_claim.sig, token_claim.proof, token_claim.leaf, {'from' : token_claim.user_address})

def test_wrong_user(token,td,set_dist_address): 
    '''
       This test case emulates a scenario where the signed message service is tricked into signing a claim 
       that doesn't exist on the master initial distribution list.
    '''

    valid_claim = BadClaim1() # get known valid claim base metadata 
    token_claim = TokenClaim(valid_claim.user_id, valid_claim.claim_address, valid_claim.delegate_address, valid_claim.total_claim) # get signed token claim from the EMSM
    
    # should revert as we send a leaf that doesn't exist on the tree 
    with brownie.reverts("TokenDistributor: Leaf Hash Mismatch."):
        td.claimTokens(token_claim.user_id, token_claim.user_address, token_claim.user_amount, token_claim.delegate_address, token_claim.hash, token_claim.sig, token_claim.proof, token_claim.leaf, {'from' : token_claim.user_address})

def _full_dist_list(token, td, seed, set_dist_address):
    '''Iterate though and test every claim on the list'''
  
    with open(env['DIST_FILE'], 'r') as csvfile:
        initial_distribution = csv.reader(csvfile)
        next(initial_distribution) # skip header 
        
        for row in initial_distribution:
            random_index = random.randint(0, 9) # pick a random number for address index 
            user_id = int(row[1]) # user_id 
            total_claim = int(row[2]) # total_claim
            claim_address = accounts[random_index].address # set random address 
            delegate_address = claim_address # self delegate 
                   
            # get balance before  
            balance_before = token.balanceOf(claim_address)
            
            # craft claim object 
            token_claim = TokenClaim(user_id, claim_address, delegate_address, total_claim)
            
            # make claim
            try: 
                claim_tx = td.claimTokens(token_claim.user_id, token_claim.user_address, token_claim.user_amount, token_claim.delegate_address, token_claim.hash, token_claim.sig, token_claim.proof, token_claim.leaf, {'from' : claim_address})
            except Exception as e:
                print(f'TokenDistribution test: There was an issue sending claim to the contract: {e}') 
        
            # get use balance before claim 
            balance_after = token.balanceOf(claim_address)
     
            assert balance_before < balance_after, "Token claim failed"
             
    # uncomment to debug and print details to stdout 
    # assert False, "You intentionally triggered execpetion to print debug info to stdout"


def test_sweep_unclaimed_drops_1(token, td, set_dist_address):
    '''
    Test that we can't move unclaimed drops to the Timelock before CONTRACT_ACTIVE period has passed 
    OR that we can sweep funds if CONTRACT_ACTIVE has not passed. Because CONTRACT_ACTIVE is hardcoded into the contract, 
    we have to adjust CONTRACT_ACTIVE on the contract to test both cases here: Set CONTRACT_ACTIVE 1 seconds to test sweep success
    leave CONTRACT_ACTIVE greater than 5 seconds (default 24 weeks) to test we can't prematurely sweep. 
    '''
    deploy_time = td.deployTime()
    drop_active = td.CONTRACT_ACTIVE()
    time.sleep(5) # sleep for 10 seconds
    current_time = chain.time()
   
    if (current_time >= deploy_time + drop_active): # airdrop is no longer live, anyone can sweep all funds to Timelock
        # test_sweep_unclaimed_drops_2 should hit, so we can just pass
        # sweep_unclaimed_drops_2(token, td, set_dist_address)
        balance_before = token.balanceOf(td)
        td.transferUnclaimed({'from': accounts[0]})
        balance_after = token.balanceOf(td)
        assert balance_before > balance_after, "Sweep Unclaimed Failed"
        assert balance_after == 0, "sweep unclaimed failed to empty the contract"
    else: # airdrop is still live, attempts to sweep should revert  
        # should revert if we try to sweep unclaimed funds before CONTRACT_ACTIVE period is up
        with brownie.reverts("TokenDistributor: Contract is still active."):
            td.transferUnclaimed({'from': accounts[0]})

def get_claim_from_dist():
    '''Pull back a single valid claim from initial dist list'''
  
    with open(env['DIST_FILE'], 'r') as csvfile:
        initial_distribution = csv.reader(csvfile)
        next(initial_distribution) # skip header 
        
        for row in initial_distribution:
            user_id = int(row[1]) # user_id 
            total_claim = int(row[2]) # total_claim
            print(f'HERE: {user_id}, {total_claim}')
            return(user_id, total_claim)

# reusable, valid claim pulled from first record in initial_dist file 
class ValidClaim:    
    def __init__(self):
        _user_id, _total_claim = get_claim_from_dist()
        self.user_id = _user_id
        self.claim_address = accounts[1].address 
        self.delegate_address = accounts[1].address
        self.total_claim = _total_claim

# reusable, bad claim contains 2x defined total_claim 
class BadClaim1:
    def __init__(self):
        _user_id, _total_claim = get_claim_from_dist()
        self.user_id = _user_id
        self.claim_address = accounts[1].address 
        self.delegate_address = accounts[1].address
        self.total_claim = _total_claim + _total_claim

# for crafting full signed, token claim objects  
class TokenClaim:
    def __init__(self, _user_id, _user_address, _delegate_address, _total_claim):
        ''' push claim objects emitted from Ethereum Message
            Signing Service into an on-chain claimable object 
        '''
        raw_claim = generate_claim(_user_id, _user_address, _delegate_address, _total_claim)
        
        self.user_id = _user_id
        self.user_address = _user_address 
        self.user_amount = _total_claim
        self.delegate_address = _delegate_address
        self.hash = raw_claim["eth_signed_message_hash_hex"]
        self.sig = raw_claim["eth_signed_signature_hex"]
        self.leaf = raw_claim["leaf"]
        self.proof = raw_claim["proof"]

def generate_claim(user_id, user_address, delegate_address, total_claim):
    '''Mimic Quadratic Lands application by sending a claim request to the Ethereum Signed Message Service'''
    
    post_data_to_emss = {}
    post_data_to_emss['user_id'] = user_id
    post_data_to_emss['user_address'] = user_address
    post_data_to_emss['delegate_address'] = delegate_address
    post_data_to_emss['user_amount'] = total_claim

    # print(f'POST DATA FOR ESMS: {json.dumps(post_data_to_emss)}')
    # create a hash of post data
    try:                 
        hmac_signed_claim = create_sha256_signature(env['DEV_HMAC_KEY'], json.dumps(post_data_to_emss))
    except: 
        print('Error creating hash of POST data for ESMS')

    header = { 
        "X-GITCOIN-SIG" : hmac_signed_claim,
        "content-type": "application/json",
    }

    # POST relevant user data to micro service that returns signed transation data for the user broadcast
    try: 
        emss_response = requests.post(env['V1_API_URL'], data=json.dumps(post_data_to_emss), headers=header)
        emss_response_content = emss_response.content
        emss_response.raise_for_status() # raise exception on error 
    except requests.exceptions.ConnectionError:
        print('TokenDistribtor: ConnectionError while connecting to ESMS')
     
    except requests.exceptions.Timeout:
        # Maybe set up for a retry
        print('TokenDistribtor: Timeout while connecting to ESMS')
 
    except requests.exceptions.TooManyRedirects:
        print('TokenDistribtor: Too many redirects while connecting to ESMS')
     
    except requests.exceptions.RequestException as e:
        # catastrophic error. bail.
        print(f'TokenDistribtor test Error posting to ESMS - {e}')
    
    try:
        # ESMS returns may retrun objects. so, we decode 
        full_response = json.loads(emss_response_content.decode('utf-8'))
    except Exception as e:
        full_response = []
        print(f'TokenDistribution test Error - {e}')

    # print(f'GTC Token Distributor - ESMS response: {full_response}')
    return full_response 
    

def create_sha256_signature(key, message):
    '''Given key & message, returns HMAC digest of the message'''
    try:
        byte_key = binascii.unhexlify(key)
        message = message.encode()
        return hmac.new(byte_key, message, hashlib.sha256).hexdigest().upper()
    except Exception as e:
        logger.error(f'TokenDistribtor - Error Hashing Message: {e}')
        return False 

